from __future__ import annotations

import asyncio
import json
import shutil
import time
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.llm.openrouter import ChatResult
from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    RemoteSessionConfig,
    RemoteShellContext,
    RunSession,
    SessionStatus,
    ShellCommandResult,
    ToolCall,
    WorkspaceRef,
    WorkspaceMode,
)
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.remote import load_remote_session_config
from opencompany.storage import Storage
from opencompany.utils import utc_now
from opencompany.workspace import WorkspaceManager


class FakeLLMClient:
    def __init__(self, responses: list[str | ChatResult]) -> None:
        self._responses = responses
        self._index = 0

    async def stream_chat(self, **kwargs) -> ChatResult:
        response = self._responses[self._index]
        self._index += 1
        on_token = kwargs.get("on_token")
        on_reasoning = kwargs.get("on_reasoning")
        if isinstance(response, ChatResult):
            text = response.content
        else:
            text = response
            response = ChatResult(content=text, raw_events=[])
        if on_reasoning and response.reasoning:
            midpoint = max(len(response.reasoning) // 2, 1)
            for chunk in (response.reasoning[:midpoint], response.reasoning[midpoint:]):
                maybe = on_reasoning(chunk)
                if hasattr(maybe, "__await__"):
                    await maybe
        if on_token and text:
            midpoint = max(len(text) // 2, 1)
            for chunk in (text[:midpoint], text[midpoint:]):
                maybe = on_token(chunk)
                if hasattr(maybe, "__await__"):
                    await maybe
        return response


class RecordingLLMClient(FakeLLMClient):
    def __init__(self, responses: list[str | ChatResult]) -> None:
        super().__init__(responses)
        self.calls: list[dict[str, object]] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        return await super().stream_chat(**kwargs)


class DebugPathAwareLLMClient(FakeLLMClient):
    def __init__(self, responses: list[str | ChatResult]) -> None:
        super().__init__(responses)
        self.request_response_log_dir: Path | None = None
        self.request_response_log_path: Path | None = None
        self.paths_seen: list[tuple[Path | None, Path | None]] = []
        self.scopes_seen: list[tuple[str, str]] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        self.paths_seen.append((self.request_response_log_dir, self.request_response_log_path))
        self.scopes_seen.append(
            (
                str(kwargs.get("debug_agent_id", "")),
                str(kwargs.get("debug_module", "")),
            )
        )
        return await super().stream_chat(**kwargs)


class BlockingWorkerLLMClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def stream_chat(self, **kwargs) -> ChatResult:
        self.calls += 1
        on_token = kwargs.get("on_token")
        if self.calls == 1:
            response = json.dumps(
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
            )
            if on_token:
                maybe = on_token(response)
                if hasattr(maybe, "__await__"):
                    await maybe
            return ChatResult(content=response, raw_events=[])
        self.started.set()
        await asyncio.Future()


async def emit_chat_response(
    kwargs: dict[str, object],
    response: str,
) -> ChatResult:
    on_token = kwargs.get("on_token")
    if on_token:
        maybe = on_token(response)
        if hasattr(maybe, "__await__"):
            await maybe
    return ChatResult(content=response, raw_events=[])


def _extract_prompt_route(initial: str) -> str:
    for raw_line in str(initial).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("User task:") or line.startswith("用户任务："):
            return "root"
        if line.startswith("Assigned instruction:") or line.startswith("分配给你的指令："):
            instruction = (
                line.removeprefix("Assigned instruction: ")
                .removeprefix("分配给你的指令：")
                .strip()
            )
            return f"worker:{instruction}"
    raise AssertionError(f"Unknown prompt route: {initial}")


def _extract_agent_name(initial: str) -> str:
    for raw_line in str(initial).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("You are ") and " (agent id:" in line:
            return line.removeprefix("You are ").split(" (agent id:", 1)[0].strip()
        if line.startswith("你是") and "(agent id:" in line:
            return line.removeprefix("你是").split("(agent id:", 1)[0].strip()
    raise AssertionError(f"Unknown agent name in prompt: {initial}")


class RootConcurrencyLLMClient:
    def __init__(self) -> None:
        self.root_calls = 0
        self.worker_calls = 0
        self.child_started = asyncio.Event()
        self.child_finished = asyncio.Event()
        self.root_wait_requested = asyncio.Event()

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        if route == "root":
            self.root_calls += 1
            if self.root_calls == 1:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Inspect",
                                    "instruction": "Inspect the repository",
                                }
                            ]
                        }
                    ),
                )
            if self.root_calls == 2:
                await asyncio.wait_for(self.child_started.wait(), timeout=1)
                self.root_wait_requested.set()
                return await emit_chat_response(
                    kwargs,
                    json.dumps({"actions": [{"type": "list_tool_runs"}]}),
                )
            if self.root_calls >= 3:
                await asyncio.wait_for(self.child_finished.wait(), timeout=1)
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Root continued while the child was running.",
                                }
                            ]
                        }
                    ),
                )
        elif route.startswith("worker:"):
            self.worker_calls += 1
            self.child_started.set()
            await asyncio.wait_for(self.root_wait_requested.wait(), timeout=1)
            self.child_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Child inspection finished.",
                                "next_recommendation": "Finalize.",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(
            f"Unexpected call sequence: route={route}, root={self.root_calls}, worker={self.worker_calls}"
        )


class WorkerConcurrencyLLMClient:
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0
        self.child_started = asyncio.Event()
        self.child_finished = asyncio.Event()
        self.parent_wait_requested = asyncio.Event()

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        if route == "worker:Parent worker task":
            self.parent_calls += 1
            if self.parent_calls == 1:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Nested child",
                                    "instruction": "Nested child task",
                                }
                            ]
                        }
                    ),
                )
            if self.parent_calls == 2:
                await asyncio.wait_for(self.child_started.wait(), timeout=1)
                self.parent_wait_requested.set()
                return await emit_chat_response(
                    kwargs,
                    json.dumps({"actions": [{"type": "list_tool_runs"}]}),
                )
            if self.parent_calls >= 3:
                await asyncio.wait_for(self.child_finished.wait(), timeout=1)
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Parent worker continued before waiting for its child.",
                                    "next_recommendation": "Report success.",
                                }
                            ]
                        }
                    ),
                )
        if route == "worker:Nested child task":
            self.child_calls += 1
            self.child_started.set()
            await asyncio.wait_for(self.parent_wait_requested.wait(), timeout=1)
            self.child_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Nested child finished.",
                                "next_recommendation": "Parent can continue.",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(
            f"Unexpected worker call sequence: parent={self.parent_calls}, child={self.child_calls}, route={route}, initial={initial}"
        )


class SiblingWorkerConcurrencyLLMClient:
    def __init__(self) -> None:
        self.root_calls = 0
        self.slow_started = asyncio.Event()
        self.fast_started = asyncio.Event()
        self.root_checked_fast = asyncio.Event()
        self.fast_finished = asyncio.Event()
        self.slow_finished = asyncio.Event()
        self.completion_order: list[str] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        if route == "root":
            self.root_calls += 1
            if self.root_calls == 1:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Slow worker",
                                    "instruction": "Slow sibling task",
                                }
                            ]
                        }
                    ),
                )
            if self.root_calls == 2:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Fast worker",
                                    "instruction": "Fast sibling task",
                                }
                            ]
                        }
                    ),
                )
            if self.root_calls == 3:
                await asyncio.wait_for(self.slow_started.wait(), timeout=1)
                await asyncio.wait_for(self.fast_started.wait(), timeout=1)
                assert not self.slow_finished.is_set()
                self.root_checked_fast.set()
                return await emit_chat_response(
                    kwargs,
                    json.dumps({"actions": [{"type": "list_tool_runs"}]}),
                )
            if self.root_calls >= 4:
                await asyncio.wait_for(self.slow_finished.wait(), timeout=1)
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Fast sibling finished while slow sibling was still running.",
                                }
                            ]
                        }
                    ),
                )
        if route == "worker:Slow sibling task":
            self.slow_started.set()
            await asyncio.wait_for(self.fast_finished.wait(), timeout=1)
            self.completion_order.append("slow")
            self.slow_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Slow sibling finished.",
                                "next_recommendation": "Parent can now finish.",
                            }
                        ]
                    }
                ),
            )
        if route == "worker:Fast sibling task":
            self.fast_started.set()
            await asyncio.wait_for(self.root_checked_fast.wait(), timeout=1)
            self.completion_order.append("fast")
            self.fast_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Fast sibling finished.",
                                "next_recommendation": "Wait for the slow sibling.",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected route for sibling concurrency test: {route}")


class MultiRootConcurrencyLLMClient:
    def __init__(self) -> None:
        self.slow_root_started = asyncio.Event()
        self.fast_root_finished = asyncio.Event()

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        if route != "root":
            raise AssertionError(f"Unexpected route for multi-root concurrency test: {route}")
        root_name = _extract_agent_name(initial)
        if root_name == "Root Slow":
            self.slow_root_started.set()
            await asyncio.wait_for(self.fast_root_finished.wait(), timeout=1)
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Root Slow finished after Root Fast.",
                            }
                        ]
                    }
                ),
            )
        if root_name == "Root Fast":
            await asyncio.wait_for(self.slow_root_started.wait(), timeout=1)
            self.fast_root_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Root Fast finished while Root Slow was still waiting.",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected root name for multi-root concurrency test: {root_name}")


class WorkerSemaphoreLLMClient:
    def __init__(self) -> None:
        self.root_calls = 0
        self.worker_one_started = asyncio.Event()
        self.worker_two_started = asyncio.Event()
        self.allow_worker_one_finish = asyncio.Event()
        self.worker_one_finished = asyncio.Event()
        self.worker_two_finished = asyncio.Event()
        self.completion_order: list[str] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        if route == "root":
            self.root_calls += 1
            if self.root_calls == 1:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Worker One",
                                    "instruction": "Semaphore worker one",
                                }
                            ]
                        }
                    ),
                )
            if self.root_calls == 2:
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "spawn_agent",
                                    "name": "Worker Two",
                                    "instruction": "Semaphore worker two",
                                }
                            ]
                        }
                    ),
                )
            if self.root_calls == 3:
                await asyncio.wait_for(self.worker_one_started.wait(), timeout=1)
                assert not self.worker_two_started.is_set()
                self.allow_worker_one_finish.set()
                return await emit_chat_response(
                    kwargs,
                    json.dumps({"actions": [{"type": "list_tool_runs"}]}),
                )
            if self.root_calls >= 4:
                await asyncio.wait_for(self.worker_two_finished.wait(), timeout=1)
                return await emit_chat_response(
                    kwargs,
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Worker semaphore limit was enforced.",
                                }
                            ]
                        }
                    ),
                )
        if route == "worker:Semaphore worker one":
            self.worker_one_started.set()
            await asyncio.wait_for(self.allow_worker_one_finish.wait(), timeout=1)
            self.completion_order.append("one")
            self.worker_one_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Worker one finished.",
                                "next_recommendation": "Start worker two.",
                            }
                        ]
                    }
                ),
            )
        if route == "worker:Semaphore worker two":
            self.worker_two_started.set()
            await asyncio.wait_for(self.worker_one_finished.wait(), timeout=1)
            self.completion_order.append("two")
            self.worker_two_finished.set()
            return await emit_chat_response(
                kwargs,
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "finish",
                                "status": "completed",
                                "summary": "Worker two finished.",
                                "next_recommendation": "Finish the root.",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(f"Unexpected route for worker semaphore test: {route}")


class RoutedLLMClient:
    def __init__(self, routes: dict[str, list[str | ChatResult]]) -> None:
        self._routes = {key: list(values) for key, values in routes.items()}
        self.calls: list[str] = []
        self.requests: list[dict[str, object]] = []

    async def stream_chat(self, **kwargs) -> ChatResult:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        initial = str(messages[1]["content"])
        route = _extract_prompt_route(initial)
        self.calls.append(route)
        self.requests.append(kwargs)
        responses = self._routes.get(route)
        if not responses:
            raise AssertionError(f"No scripted response left for route {route}")
        response = responses.pop(0)
        if isinstance(response, ChatResult):
            payload = response.content
        else:
            payload = response
            response = ChatResult(content=payload, raw_events=[])
        on_token = kwargs.get("on_token")
        if on_token and payload:
            maybe = on_token(payload)
            if hasattr(maybe, "__await__"):
                await maybe
        return response


def tool_call_result(name: str, arguments: dict[str, object], call_id: str) -> ChatResult:
    return ChatResult(
        content="",
        raw_events=[],
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments_json=json.dumps(arguments),
            )
        ],
    )


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
temperature = 0.1
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


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_request_response_log_path_is_session_scoped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(
                project_dir,
                locale="en",
                app_dir=project_dir,
                debug=True,
            )
            llm_client = DebugPathAwareLLMClient(
                [
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
                ]
            )
            orchestrator.llm_client = llm_client

            session = await orchestrator.run_task("Run with debug logging")
            expected_path = orchestrator.paths.session_logs_path(
                session.id,
                "debug/requests_responses.jsonl",
            )
            expected_dir = orchestrator.paths.session_dir(session.id, create=True) / "debug"

            self.assertEqual(llm_client.request_response_log_dir, expected_dir)
            self.assertEqual(llm_client.request_response_log_path, expected_path)
            self.assertTrue(llm_client.paths_seen)
            self.assertTrue(
                all(
                    seen_dir == expected_dir and seen_path == expected_path
                    for seen_dir, seen_path in llm_client.paths_seen
                )
            )
            self.assertTrue(llm_client.scopes_seen)
            self.assertTrue(all(scope[0].startswith("agent-") for scope in llm_client.scopes_seen))

    async def test_debug_timings_are_written_per_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(
                project_dir,
                locale="en",
                app_dir=project_dir,
                debug=True,
            )
            orchestrator.llm_client = FakeLLMClient(
                [
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
                ]
            )

            session = await orchestrator.run_task("Run with debug timings")
            timings_path = orchestrator.paths.session_logs_path(
                session.id,
                "debug/timings.jsonl",
            )
            self.assertTrue(timings_path.exists())
            records = [
                json.loads(line)
                for line in timings_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(records)
            stages = {str(record.get("stage", "")).strip() for record in records}
            self.assertIn("ask_agent.llm_roundtrip", stages)
            self.assertIn("execute_action.submit", stages)
            self.assertIn("tool_run.execute", stages)
            for record in records:
                self.assertGreaterEqual(int(record.get("duration_ms", -1)), 0)

    async def test_subscriber_errors_do_not_abort_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Completed with noisy subscriber.",
                                }
                            ]
                        }
                    )
                ]
            )

            def broken_subscriber(payload: dict[str, object]) -> None:
                del payload
                raise RuntimeError("subscriber failed")

            orchestrator.subscribe(broken_subscriber)
            session = await orchestrator.run_task("Finalize even if subscriber fails")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Completed with noisy subscriber.")

    async def test_subscriber_cancelled_error_does_not_abort_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Completed despite cancelled subscriber.",
                                }
                            ]
                        }
                    )
                ]
            )

            def cancelled_subscriber(payload: dict[str, object]) -> None:
                del payload
                raise asyncio.CancelledError()

            orchestrator.subscribe(cancelled_subscriber)
            session = await orchestrator.run_task("Finalize even if subscriber is cancelled")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "Completed despite cancelled subscriber.")

    def test_invalid_session_id_is_rejected_without_creating_session_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            build_test_project(app_dir)
            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            sessions_dir = (app_dir / ".opencompany" / "sessions").resolve()
            before = sorted(path.name for path in sessions_dir.iterdir() if path.is_dir())

            with self.assertRaises(ValueError):
                orchestrator.load_session_events("../escape")
            with self.assertRaises(ValueError):
                orchestrator.project_sync_status("bad/session")

            after = sorted(path.name for path in sessions_dir.iterdir() if path.is_dir())
            self.assertEqual(before, after)

    async def test_llm_requests_send_tools_via_api_fields(self) -> None:
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
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            routed_requests = list(zip(client.calls, client.requests, strict=True))
            root_request = next(request["messages"] for route, request in routed_requests if route == "root")
            worker_request = next(
                request["messages"]
                for route, request in routed_requests
                if route == "worker:Inspect the repository"
            )
            assert isinstance(root_request, list)
            assert isinstance(worker_request, list)
            self.assertNotIn("Implemented tools and exact action shapes", root_request[1]["content"])
            self.assertNotIn("Implemented tools and exact action shapes", worker_request[1]["content"])
            root_request_payload = next(
                request for route, request in routed_requests if route == "root"
            )
            worker_request_payload = next(
                request
                for route, request in routed_requests
                if route == "worker:Inspect the repository"
            )
            self.assertIn("tools", root_request_payload)
            self.assertIn("tool_choice", root_request_payload)
            self.assertIn("parallel_tool_calls", root_request_payload)
            self.assertTrue(root_request_payload["parallel_tool_calls"])
            self.assertEqual(root_request_payload["tool_choice"], "auto")
            root_tool_names = {
                tool["function"]["name"]
                for tool in root_request_payload["tools"]
            }
            worker_tool_names = {
                tool["function"]["name"]
                for tool in worker_request_payload["tools"]
            }
            self.assertIn("spawn_agent", root_tool_names)
            self.assertIn("finish", root_tool_names)
            self.assertIn("shell", root_tool_names)
            self.assertIn("shell", worker_tool_names)
            self.assertIn("finish", worker_tool_names)

    async def test_run_task_model_override_applies_to_all_agent_calls(self) -> None:
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
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project", model="openai/gpt-4.1")

            self.assertEqual(session.status.value, "completed")
            self.assertTrue(client.requests)
            for request in client.requests:
                self.assertEqual(request["model"], "openai/gpt-4.1")
            llm_snapshot = (
                (session.config_snapshot or {})
                .get("llm", {})
                .get("openrouter", {})
            )
            self.assertEqual(llm_snapshot.get("model"), "openai/gpt-4.1")
            self.assertEqual(llm_snapshot.get("coordinator_model"), "openai/gpt-4.1")
            self.assertEqual(llm_snapshot.get("worker_model"), "openai/gpt-4.1")
            session_agents = orchestrator.load_session_agents(session.id)
            self.assertGreaterEqual(len(session_agents), 2)
            for agent_payload in session_agents:
                self.assertEqual(agent_payload.get("model"), "openai/gpt-4.1")

    async def test_spawn_child_assigns_unique_name_and_identity_prefixed_prompt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-spawn-identity"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Plan work",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                children=["agent-worker-existing"],
                conversation=[{"role": "user", "content": "Plan work"}],
            )
            existing_worker_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-worker-existing")
            existing_worker = AgentNode(
                id="agent-worker-existing",
                session_id=session_id,
                name="Worker Agent",
                role=AgentRole.WORKER,
                instruction="previous worker task",
                workspace_id=existing_worker_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.COMPLETED,
                completion_status="completed",
                conversation=[{"role": "user", "content": "previous worker task"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="spawn identity test",
                locale="en",
                root_agent_id=root.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(existing_worker)
            orchestrator._sync_agent_messages(root)
            orchestrator._sync_agent_messages(existing_worker)
            agents = {
                root.id: root,
                existing_worker.id: existing_worker,
            }

            child_id = orchestrator._spawn_child(
                parent=root,
                action={
                    "type": "spawn_agent",
                    "name": "Worker Agent",
                    "instruction": "Inspect the repository",
                },
                agents=agents,
                workspace_manager=workspace_manager,
            )

            self.assertIsNotNone(child_id)
            assert child_id is not None
            self.assertNotEqual(child_id, existing_worker.id)
            self.assertIn(child_id, agents)
            child = agents[child_id]
            self.assertEqual(child.name, "Worker Agent (2)")
            self.assertTrue(child.conversation)
            self.assertTrue(
                str(child.conversation[0].get("content", "")).startswith(
                    f"You are Worker Agent (2) (agent id: {child_id}).\n"
                    f"Your parent agent is {root.name} (agent id: {root.id}).\n"
                )
            )
            self.assertIn(
                "Assigned instruction: Inspect the repository",
                str(child.conversation[0].get("content", "")),
            )

    async def test_spawn_child_allows_same_instruction_after_cancelled_child(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-spawn-after-cancelled"
            now = utc_now()
            workspace_manager = WorkspaceManager(orchestrator.paths.session_dir(session_id))
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Plan work",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
                children=["agent-worker-cancelled"],
                conversation=[{"role": "user", "content": "Plan work"}],
            )
            cancelled_worker_workspace = workspace_manager.fork_workspace(
                root_workspace.id,
                "agent-worker-cancelled",
            )
            cancelled_worker = AgentNode(
                id="agent-worker-cancelled",
                session_id=session_id,
                name="Worker Agent",
                role=AgentRole.WORKER,
                instruction="Inspect the repository",
                workspace_id=cancelled_worker_workspace.id,
                parent_agent_id=root.id,
                status=AgentStatus.CANCELLED,
                completion_status="cancelled",
                conversation=[{"role": "user", "content": "Inspect the repository"}],
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="spawn after cancelled",
                locale="en",
                root_agent_id=root.id,
                status=SessionStatus.RUNNING,
                created_at=now,
                updated_at=now,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root)
            orchestrator.storage.upsert_agent(cancelled_worker)
            agents = {
                root.id: root,
                cancelled_worker.id: cancelled_worker,
            }

            child_id = orchestrator._spawn_child(
                parent=root,
                action={
                    "type": "spawn_agent",
                    "name": "Worker Agent",
                    "instruction": "Inspect the repository",
                },
                agents=agents,
                workspace_manager=workspace_manager,
            )

            self.assertIsNotNone(child_id)
            assert child_id is not None
            self.assertNotEqual(child_id, cancelled_worker.id)
            self.assertEqual(agents[child_id].status, AgentStatus.PENDING)

    async def test_llm_requests_localize_tool_definitions_from_locale(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="zh", app_dir=project_dir)
            client = RecordingLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "已完成。",
                                }
                            ]
                        }
                    )
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("检查工具定义语言")

            self.assertEqual(session.status.value, "completed")
            wait_time = next(
                tool
                for tool in client.calls[0]["tools"]
                if tool["function"]["name"] == "wait_time"
            )
            self.assertEqual(
                wait_time["function"]["description"],
                "等待一段受限时长。",
            )
            self.assertEqual(
                wait_time["function"]["parameters"]["properties"]["seconds"]["description"],
                "必填等待秒数，必须 >= 10 且 <= 60。",
            )

    async def test_orchestrator_loads_runtime_assets_from_app_dir(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "opencompany-app"
            project_dir = root / "target-project"
            app_dir.mkdir()
            project_dir.mkdir()
            build_test_project(app_dir)
            (app_dir / ".env").write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")
            (project_dir / "README.md").write_text("target\n", encoding="utf-8")

            orchestrator = Orchestrator(project_dir, locale="en", app_dir=app_dir)
            self.assertIsNotNone(orchestrator.llm_client)
            self.assertEqual(orchestrator.paths.data_dir, (app_dir / ".opencompany").resolve())

            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Used app_dir config.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task(
                "Inspect this target project",
                workspace_mode="staged",
            )
            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.project_dir, project_dir.resolve())
            self.assertTrue(
                (orchestrator.paths.session_dir(session.id) / "snapshots" / "root" / "README.md").exists()
            )
            self.assertTrue(
                (orchestrator.paths.session_dir(session.id) / "events.jsonl").exists()
            )
            self.assertTrue(
                orchestrator.paths.session_agent_messages_path(session.id, session.root_agent_id).exists()
            )

    async def test_direct_mode_uses_live_root_workspace_and_disables_project_sync(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Completed in direct mode.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.workspace_mode, WorkspaceMode.DIRECT)
            session_dir = orchestrator.paths.session_dir(session.id)
            self.assertFalse((session_dir / "snapshots" / "root").exists())
            self.assertFalse((session_dir / "snapshots" / "root_base").exists())
            checkpoint = orchestrator.storage.latest_checkpoint(session.id)
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            self.assertEqual(
                checkpoint["state"]["workspaces"]["root"]["path"],
                str(project_dir.resolve()),
            )
            self.assertEqual(
                checkpoint["state"]["workspaces"]["root"]["base_snapshot_path"],
                str(project_dir.resolve()),
            )
            self.assertEqual(
                orchestrator.project_sync_status(session.id)["status"],
                "disabled",
            )
            with self.assertRaises(ValueError):
                orchestrator.project_sync_preview(session.id)
            with self.assertRaises(ValueError):
                orchestrator.apply_project_sync(session.id)
            with self.assertRaises(ValueError):
                orchestrator.undo_project_sync(session.id)

    async def test_run_task_rejects_remote_workspace_in_staged_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            with self.assertRaisesRegex(ValueError, "direct mode"):
                await orchestrator.run_task(
                    "Inspect this project",
                    workspace_mode=WorkspaceMode.STAGED,
                    remote_config={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com",
                        "remote_dir": "/home/demo/workspace",
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                )

    async def test_run_task_persists_remote_session_config_without_password(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            remote_workspace = project_dir / "remote-workspace"
            remote_workspace.mkdir(parents=True, exist_ok=True)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Completed in remote direct mode.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task(
                "Inspect remote workspace",
                workspace_mode=WorkspaceMode.DIRECT,
                remote_config={
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com:2222",
                    "remote_dir": str(remote_workspace),
                    "auth_mode": "password",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                },
                remote_password="secret-pass",
            )

            session_dir = orchestrator.paths.session_dir(session.id)
            loaded = load_remote_session_config(session_dir)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.ssh_target, "demo@example.com:2222")
            self.assertEqual(
                Path(loaded.remote_dir).resolve(),
                remote_workspace.resolve(),
            )
            self.assertEqual(loaded.auth_mode, "password")

            raw_remote = (session_dir / "remote_session.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-pass", raw_remote)
            self.assertEqual(session.project_dir, remote_workspace)

    async def test_run_task_remote_setup_failure_still_persists_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            remote_workspace = project_dir / "remote-workspace"
            remote_workspace.mkdir(parents=True, exist_ok=True)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            def _raise_remote_setup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                del args, kwargs
                raise RuntimeError("remote setup failed")

            orchestrator._apply_session_remote_runtime = _raise_remote_setup  # type: ignore[method-assign]

            with self.assertRaisesRegex(RuntimeError, "remote setup failed"):
                await orchestrator.run_task(
                    "Inspect remote workspace",
                    workspace_mode=WorkspaceMode.DIRECT,
                    remote_config={
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:2222",
                        "remote_dir": str(remote_workspace),
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    },
                )

            assert orchestrator.latest_session_id is not None
            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            session_row = storage.load_session(orchestrator.latest_session_id)
            self.assertIsNotNone(session_row)
            assert session_row is not None
            self.assertEqual(str(session_row.get("status")), SessionStatus.FAILED.value)

            events = storage.load_events(orchestrator.latest_session_id)
            event_types = [str(event.get("event_type")) for event in events]
            self.assertIn("session_started", event_types)
            self.assertIn("session_failed", event_types)

            agents = storage.load_agents(orchestrator.latest_session_id)
            self.assertTrue(agents)
            self.assertEqual(str(agents[0].get("role")), AgentRole.ROOT.value)

    def test_apply_session_remote_runtime_uses_stored_password_ref(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            remote_config = RemoteSessionConfig(
                kind="remote_ssh",
                ssh_target="demo@example.com:22",
                remote_dir="/home/demo/workspace",
                auth_mode="password",
                known_hosts_policy="accept_new",
                remote_os="linux",
                password_ref="ref-session-1",
            )

            with mock.patch(
                "opencompany.orchestrator.load_remote_session_password",
                return_value="secret-pass",
            ) as load_password:
                orchestrator._apply_session_remote_runtime(
                    session_id="session-remote",
                    remote_config=remote_config,
                    remote_password=None,
                    require_password=True,
                )

            load_password.assert_called_once_with("ref-session-1")
            context = orchestrator.tool_executor.session_remote_context("session-remote")
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context.password, "secret-pass")

    def test_apply_session_remote_runtime_generates_password_ref_on_first_password(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), locale="en", app_dir=app_dir)
            remote_config = RemoteSessionConfig(
                kind="remote_ssh",
                ssh_target="demo@example.com:22",
                remote_dir="/home/demo/workspace",
                auth_mode="password",
                known_hosts_policy="accept_new",
                remote_os="linux",
            )

            with (
                mock.patch(
                    "opencompany.orchestrator.build_remote_password_ref",
                    return_value="ref-session-1",
                ) as build_ref,
                mock.patch("opencompany.orchestrator.save_remote_session_password") as save_password,
                mock.patch.object(orchestrator, "_persist_session_remote_config") as persist_config,
            ):
                orchestrator._apply_session_remote_runtime(
                    session_id="session-remote",
                    remote_config=remote_config,
                    remote_password="secret-pass",
                    require_password=True,
                )

            build_ref.assert_called_once()
            save_password.assert_called_once_with("ref-session-1", "secret-pass")
            persist_config.assert_called_once_with("session-remote", remote_config)
            self.assertEqual(remote_config.password_ref, "ref-session-1")
            context = orchestrator.tool_executor.session_remote_context("session-remote")
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context.password, "secret-pass")

    def test_spawn_child_reuses_root_workspace_in_direct_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            session_id = "session-direct-child"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir, mode="direct")
            parent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Inspect the repo",
                workspace_id=root_workspace.id,
                status=AgentStatus.RUNNING,
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir,
                task="Inspect the repo",
                locale="en",
                root_agent_id=parent.id,
                workspace_mode=WorkspaceMode.DIRECT,
                status=SessionStatus.RUNNING,
            )
            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(parent)
            orchestrator.tool_executor.set_project_dir(project_dir)
            agents = {parent.id: parent}

            child_id = orchestrator.tool_executor.spawn_child(
                parent=parent,
                action={
                    "type": "spawn_agent",
                    "name": "Inspect Worker",
                    "instruction": "Inspect the repository",
                },
                agents=agents,
                workspace_manager=workspace_manager,
                new_agent_id=orchestrator._new_agent_id,
                worker_initial_message=orchestrator._worker_initial_message,
            )

            self.assertIsNotNone(child_id)
            assert child_id is not None
            self.assertEqual(agents[child_id].workspace_id, root_workspace.id)
            self.assertEqual(workspace_manager.root_workspace().path, project_dir.resolve())

    async def test_session_jsonl_and_message_logs_preserve_utf8_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="zh", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "已完成中文总结。",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                ]
            )

            session = await orchestrator.run_task("检查中文日志")

            events_path = orchestrator.paths.session_logs_path(session.id, "events.jsonl")
            events_text = events_path.read_text(encoding="utf-8")
            self.assertIn("检查中文日志", events_text)
            self.assertIn("已完成中文总结。", events_text)
            self.assertNotIn("\\u68c0\\u67e5", events_text)
            self.assertNotIn("\\u5df2\\u5b8c\\u6210", events_text)

            messages_path = orchestrator.paths.session_agent_messages_path(
                session.id,
                session.root_agent_id,
            )
            messages_text = messages_path.read_text(encoding="utf-8")
            self.assertIn("检查中文日志", messages_text)
            self.assertIn("已完成中文总结。", messages_text)
            self.assertNotIn("\\u68c0\\u67e5", messages_text)
            self.assertNotIn("\\u5df2\\u5b8c\\u6210", messages_text)

    async def test_agent_message_logs_store_complete_messages_not_stream_fragments(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            root_spawn = json.dumps(
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
            )
            worker_complete = json.dumps(
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
            root_finalize = json.dumps(
                {
                    "actions": [
                        {
                            "type": "finish",
                            "status": "completed",
                            "summary": "All work completed.",
                        }
                    ]
                }
            )
            root_wait = json.dumps(
                {
                    "actions": [
                        {
                            "type": "wait_time",
                            "seconds": 10,
                        }
                    ]
                }
            )
            orchestrator.llm_client = RoutedLLMClient(
                {
                    "root": [root_spawn, root_wait, root_finalize],
                    "worker:Inspect the repository": [worker_complete],
                }
            )

            session = await orchestrator.run_task("Inspect this project")

            root_messages_path = orchestrator.paths.session_agent_messages_path(
                session.id,
                session.root_agent_id,
            )
            root_records = [
                json.loads(line)
                for line in root_messages_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([record["message_index"] for record in root_records], list(range(len(root_records))))
            root_assistant_messages = [
                record["message"]["content"]
                for record in root_records
                if record["role"] == "assistant"
            ]
            self.assertEqual(root_assistant_messages, [root_spawn, root_wait, root_finalize])

            worker_messages_paths = sorted(
                orchestrator.paths.session_dir(session.id).glob("agent-*_messages.jsonl")
            )
            self.assertEqual(len(worker_messages_paths), 2)
            worker_path = next(
                path for path in worker_messages_paths if path != root_messages_path
            )
            worker_records = [
                json.loads(line)
                for line in worker_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertGreaterEqual(len(worker_records), 2)
            self.assertEqual(worker_records[0]["role"], "user")
            self.assertEqual(worker_records[1]["role"], "assistant")
            self.assertEqual(worker_records[1]["message"]["content"], worker_complete)

    async def test_api_request_messages_match_persisted_conversation_prefixes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RecordingLLMClient(
                [
                    json.dumps({"actions": [{"type": "list_agent_runs"}]}),
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Completed after validating history reuse.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Validate message consistency")

            root_messages_path = orchestrator.paths.session_agent_messages_path(
                session.id,
                session.root_agent_id,
            )
            root_records = [
                json.loads(line)
                for line in root_messages_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            persisted_messages = [record["message"] for record in root_records]
            self.assertGreaterEqual(len(persisted_messages), 3)

            request_histories = []
            for call in client.calls:
                request_messages = call.get("messages")
                self.assertIsInstance(request_messages, list)
                request_history = list(request_messages)[1:]
                request_histories.append(request_history)
                self.assertEqual(
                    request_history,
                    persisted_messages[: len(request_history)],
                )
            self.assertEqual(len(request_histories), 2)
            self.assertEqual(request_histories[0], persisted_messages[:1])
            self.assertEqual(
                request_histories[-1],
                persisted_messages[: len(request_histories[-1])],
            )
            self.assertEqual(root_records[-1]["role"], "user")

            prompt_events = [
                event
                for event in orchestrator.load_session_events(session.id)
                if event.get("agent_id") == session.root_agent_id
                and event.get("event_type") == "agent_prompt"
            ]
            self.assertEqual(len(prompt_events), len(request_histories))
            for call, event, request_history in zip(
                client.calls,
                prompt_events,
                request_histories,
                strict=True,
            ):
                payload = event.get("payload", {})
                self.assertIsInstance(payload, dict)
                self.assertEqual(payload.get("messages"), request_history)
                self.assertEqual(payload.get("request_messages"), call.get("messages"))

    async def test_agent_message_logs_persist_reasoning_field(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    ChatResult(
                        content=json.dumps(
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
                        raw_events=[],
                        reasoning="First inspect the repo, then finalize.",
                        reasoning_details=[
                            {
                                "type": "reasoning.text",
                                "text": "First inspect the repo, then finalize.",
                            }
                        ],
                        response_id="gen-123",
                        created=1741478400,
                        model="openai/gpt-4o-mini",
                        object="chat.completion.chunk",
                        system_fingerprint="fp_123",
                        provider="OpenAI",
                        usage={
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                        finish_reason="stop",
                        native_finish_reason="stop",
                    )
                ]
            )

            session = await orchestrator.run_task("Inspect this project")

            root_messages_path = orchestrator.paths.session_agent_messages_path(
                session.id,
                session.root_agent_id,
            )
            root_records = [
                json.loads(line)
                for line in root_messages_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assistant_record = next(record for record in root_records if record["role"] == "assistant")
            self.assertEqual(
                assistant_record["message"]["reasoning"],
                "First inspect the repo, then finalize.",
            )
            self.assertEqual(
                assistant_record["message"]["reasoning_details"],
                [
                    {
                        "type": "reasoning.text",
                        "text": "First inspect the repo, then finalize.",
                    }
                ],
            )
            self.assertEqual(assistant_record["source"], "llm")
            self.assertIn("started_at", assistant_record)
            self.assertIn("completed_at", assistant_record)
            self.assertIsInstance(assistant_record["duration_ms"], int)
            self.assertGreaterEqual(assistant_record["duration_ms"], 0)
            self.assertEqual(
                assistant_record["stream"],
                {"event_count": 0, "response_object": "chat.completion.chunk"},
            )
            self.assertNotIn("raw_events", assistant_record)
            self.assertNotIn("chunks", assistant_record["stream"])
            self.assertNotIn("events", assistant_record["stream"])
            self.assertNotIn("tokens", assistant_record["stream"])
            self.assertEqual(
                assistant_record["response"],
                {
                    "id": "gen-123",
                    "created": 1741478400,
                    "model": "openai/gpt-4o-mini",
                    "object": "chat.completion",
                    "system_fingerprint": "fp_123",
                    "provider": "OpenAI",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
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
                                "reasoning": "First inspect the repo, then finalize.",
                                "reasoning_details": [
                                    {
                                        "type": "reasoning.text",
                                        "text": "First inspect the repo, then finalize.",
                                    }
                                ],
                            },
                            "finish_reason": "stop",
                            "native_finish_reason": "stop",
                        }
                    ],
                },
            )
            checkpoint_conversation = (
                orchestrator.storage.latest_checkpoint(session.id)["state"]["agents"][session.root_agent_id]["conversation"]
            )
            self.assertEqual(
                checkpoint_conversation[1]["reasoning"],
                "First inspect the repo, then finalize.",
            )
            self.assertEqual(
                checkpoint_conversation[1]["reasoning_details"],
                [
                    {
                        "type": "reasoning.text",
                        "text": "First inspect the repo, then finalize.",
                    }
                ],
            )

            events = Storage(project_dir / ".opencompany" / "opencompany.db").load_events(session.id)
            reasoning_events = [
                json.loads(event["payload_json"])["token"]
                for event in events
                if event["event_type"] == "llm_reasoning"
            ]
            self.assertEqual(len(reasoning_events), 2)
            self.assertEqual("".join(reasoning_events), "First inspect the repo, then finalize.")

    async def test_follow_up_requests_replay_reasoning_from_prior_assistant_messages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            client = RecordingLLMClient(
                [
                    ChatResult(
                        content=json.dumps({"actions": [{"type": "list_agent_runs"}]}),
                        raw_events=[],
                        reasoning="List agent runs before finalizing.",
                        reasoning_details=[
                            {
                                "type": "reasoning.text",
                                "text": "List agent runs before finalizing.",
                            }
                        ],
                    ),
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Repository inspection complete.",
                                }
                            ]
                        }
                    ),
                ]
            )
            orchestrator.llm_client = client

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(len(client.calls), 2)
            follow_up_messages = client.calls[1]["messages"]
            assert isinstance(follow_up_messages, list)
            prior_assistant_message = follow_up_messages[2]
            self.assertEqual(prior_assistant_message["role"], "assistant")
            self.assertEqual(
                prior_assistant_message["reasoning"],
                "List agent runs before finalizing.",
            )
            self.assertEqual(
                prior_assistant_message["reasoning_details"],
                [
                    {
                        "type": "reasoning.text",
                        "text": "List agent runs before finalizing.",
                    }
                ],
            )

    async def test_root_can_list_agent_runs_before_finish(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "list_agent_runs",
                                },
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Agent runs listed.",
                                },
                            ]
                        }
                    )
                ]
            )
            session = await orchestrator.run_task("List agent runs")
            self.assertEqual(session.final_summary, "Agent runs listed.")

    def test_default_app_dir_discovers_repo_root_from_nested_code_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "moved-repo"
            nested = root / "packages" / "runtime" / "src" / "opencompany"
            nested.mkdir(parents=True)
            (root / "opencompany.toml").write_text("[project]\nname='OpenCompany'\n", encoding="utf-8")
            (root / "prompts").mkdir()
            (root / "src" / "opencompany").mkdir(parents=True)
            fake_file = nested / "orchestrator.py"
            fake_file.write_text("", encoding="utf-8")

            self.assertEqual(default_app_dir(fake_file), root.resolve())

    async def test_worker_shell_maps_absolute_target_path_into_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-shell-map")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-shell-map",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Inspect cwd mapping",
                workspace_id=child_workspace.id,
            )

            captured: dict[str, Path] = {}

            async def fake_run_command(self, request, on_event=None):
                captured["cwd"] = request.cwd
                captured["workspace_root"] = request.workspace_root
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok\n",
                    stderr="",
                    command=request.command,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                result = await orchestrator._execute_shell_action(
                    agent,
                    {"type": "shell", "command": "pwd", "cwd": str(project_dir)},
                    workspace_manager,
                )

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(captured["cwd"], child_workspace.path)
            self.assertEqual(captured["workspace_root"], child_workspace.path)

    async def test_remote_worker_shell_uses_remote_config_workspace_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-remote-shell-root"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            contaminated_workspace_path = Path("/System/Volumes/Data/home/ubuntu/test")
            workspace_manager.register(
                WorkspaceRef(
                    id="root",
                    path=contaminated_workspace_path,
                    base_snapshot_path=contaminated_workspace_path,
                    parent_workspace_id=None,
                    readonly=False,
                )
            )
            agent = AgentNode(
                id="agent-remote",
                session_id=session_id,
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Write remote file",
                workspace_id="root",
            )
            orchestrator.tool_executor.set_session_remote_config(
                session_id,
                RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/ubuntu/test",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            captured: dict[str, object] = {}

            async def fake_run_command(self, request, on_event=None):  # type: ignore[no-untyped-def]
                del on_event
                captured["cwd"] = request.cwd
                captured["workspace_root"] = request.workspace_root
                captured["writable_paths"] = list(request.writable_paths)
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok\n",
                    stderr="",
                    command=request.command,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                result = await orchestrator._execute_shell_action(
                    agent,
                    {"type": "shell", "command": "echo hi > difficulty_config.json", "cwd": "."},
                    workspace_manager,
                )

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(str(captured["workspace_root"]), "/home/ubuntu/test")
            self.assertEqual(str(captured["cwd"]), "/home/ubuntu/test")
            self.assertEqual(
                [str(path) for path in captured["writable_paths"]],  # type: ignore[index]
                ["/home/ubuntu/test"],
            )

    async def test_worker_completion_promotes_changes_to_parent_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-worker-promotion"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            worker_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-worker")
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="Coordinate",
                workspace_id=root_workspace.id,
                children=["agent-worker"],
            )
            worker_agent = AgentNode(
                id="agent-worker",
                session_id=session_id,
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Write file",
                workspace_id=worker_workspace.id,
                parent_agent_id=root_agent.id,
            )
            agents = {root_agent.id: root_agent, worker_agent.id: worker_agent}
            session = RunSession(
                id=session_id,
                project_dir=project_dir.resolve(),
                task="Create file",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
            )

            (worker_workspace.path / "snake.html").write_text("<html>snake</html>\n", encoding="utf-8")

            await orchestrator._complete_worker(
                session=session,
                agent=worker_agent,
                payload={
                    "status": "completed",
                    "summary": "Wrote snake.html",
                    "next_recommendation": "Finalize",
                },
                workspace_manager=workspace_manager,
                agents=agents,
                root_loop=0,
            )

            promoted_path = root_workspace.path / "snake.html"
            self.assertTrue(promoted_path.exists())
            self.assertEqual(promoted_path.read_text(encoding="utf-8"), "<html>snake</html>\n")

    async def test_finalize_stages_project_sync_then_apply_and_undo(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            (project_dir / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-project-sync"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="Finalize",
                workspace_id=root_workspace.id,
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir.resolve(),
                task="Sync root workspace",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
            )

            (root_workspace.path / "README.md").write_text("updated\n", encoding="utf-8")
            (root_workspace.path / "generated" / "result.txt").parent.mkdir(parents=True, exist_ok=True)
            (root_workspace.path / "generated" / "result.txt").write_text("done\n", encoding="utf-8")
            (root_workspace.path / "obsolete.txt").unlink()

            await orchestrator._finalize_root(
                session=session,
                root_agent=root_agent,
                payload={
                    "status": "completed",
                    "summary": "Completed.",
                },
                agents={root_agent.id: root_agent},
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )

            # finalize only stages project sync; it should not mutate project files directly.
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "demo\n")
            self.assertFalse((project_dir / "generated" / "result.txt").exists())
            self.assertTrue((project_dir / "obsolete.txt").exists())

            sync_state = orchestrator.project_sync_status(session_id)
            self.assertIsNotNone(sync_state)
            assert sync_state is not None
            self.assertEqual(sync_state["status"], "pending")
            self.assertEqual(set(sync_state["added"]), {"generated/result.txt"})
            self.assertEqual(set(sync_state["modified"]), {"README.md"})
            self.assertEqual(set(sync_state["deleted"]), {"obsolete.txt"})

            apply_result = orchestrator.apply_project_sync(session_id)
            self.assertEqual(apply_result["status"], "applied")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "updated\n")
            self.assertTrue((project_dir / "generated" / "result.txt").exists())
            self.assertFalse((project_dir / "obsolete.txt").exists())

            undo_result = orchestrator.undo_project_sync(session_id)
            self.assertEqual(undo_result["status"], "reverted")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "demo\n")
            self.assertFalse((project_dir / "generated" / "result.txt").exists())
            self.assertEqual((project_dir / "obsolete.txt").read_text(encoding="utf-8"), "remove me\n")

    async def test_project_sync_preview_and_apply_fallback_to_staged_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-sync-fallback"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="Finalize",
                workspace_id=root_workspace.id,
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir.resolve(),
                task="Sync root workspace",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
            )

            # Keep base/workspace identical so runtime diff computes empty.
            (root_workspace.path / "README.md").write_text("fallback-updated\n", encoding="utf-8")
            (root_workspace.base_snapshot_path / "README.md").write_text("fallback-updated\n", encoding="utf-8")

            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            await orchestrator._checkpoint(
                session=session,
                agents={root_agent.id: root_agent},
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )
            orchestrator._save_project_sync_state(
                session_id,
                {
                    "version": 1,
                    "session_id": session_id,
                    "project_dir": str(project_dir.resolve()),
                    "workspace_id": root_workspace.id,
                    "status": "pending",
                    "added": [],
                    "modified": ["README.md"],
                    "deleted": [],
                    "staged_at": "2026-03-09T00:00:00+00:00",
                    "applied_at": None,
                    "reverted_at": None,
                    "backup_dir": None,
                    "last_error": None,
                },
            )

            preview = orchestrator.project_sync_preview(session_id)
            self.assertEqual(preview["status"], "pending")
            self.assertEqual(preview["modified_count"], 1)
            self.assertEqual(preview["files"][0]["path"], "README.md")

            apply_result = orchestrator.apply_project_sync(session_id)
            self.assertEqual(apply_result["status"], "applied")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "fallback-updated\n")

            undo_result = orchestrator.undo_project_sync(session_id)
            self.assertEqual(undo_result["status"], "reverted")
            self.assertEqual((project_dir / "README.md").read_text(encoding="utf-8"), "demo\n")

    async def test_project_sync_preview_marks_binary_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            (project_dir / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0before")
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_id = "session-sync-binary-preview"
            session_dir = orchestrator.paths.session_dir(session_id)
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            root_agent = AgentNode(
                id="agent-root",
                session_id=session_id,
                name="Root",
                role=AgentRole.ROOT,
                instruction="Preview binary change",
                workspace_id=root_workspace.id,
            )
            session = RunSession(
                id=session_id,
                project_dir=project_dir.resolve(),
                task="Preview binary change",
                locale="en",
                root_agent_id=root_agent.id,
                status=SessionStatus.RUNNING,
                created_at="2026-03-09T00:00:00+00:00",
                updated_at="2026-03-09T00:00:00+00:00",
            )

            (root_workspace.path / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0after!")

            orchestrator.storage.upsert_session(session)
            orchestrator.storage.upsert_agent(root_agent)
            await orchestrator._checkpoint(
                session=session,
                agents={root_agent.id: root_agent},
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )
            orchestrator._save_project_sync_state(
                session_id,
                {
                    "version": 1,
                    "session_id": session_id,
                    "project_dir": str(project_dir.resolve()),
                    "workspace_id": root_workspace.id,
                    "status": "pending",
                    "added": [],
                    "modified": ["sample.doc"],
                    "deleted": [],
                    "staged_at": "2026-03-09T00:00:00+00:00",
                    "applied_at": None,
                    "reverted_at": None,
                    "backup_dir": None,
                    "last_error": None,
                },
            )

            preview = orchestrator.project_sync_preview(session_id)
            self.assertEqual(preview["status"], "pending")
            self.assertEqual(preview["modified_count"], 1)
            self.assertEqual(preview["files"][0]["path"], "sample.doc")
            self.assertTrue(preview["files"][0]["is_binary"])
            self.assertEqual(preview["files"][0]["patch"], "")
            self.assertEqual(preview["files"][0]["before_size"], 10)
            self.assertEqual(preview["files"][0]["after_size"], 10)

    async def test_worker_shell_rejects_cwd_outside_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "project"
            outside_dir = root / "outside"
            project_dir.mkdir()
            outside_dir.mkdir()
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-shell-reject")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-shell-reject",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Reject invalid cwd",
                workspace_id=child_workspace.id,
            )

            result = await orchestrator._execute_shell_action(
                agent,
                {"type": "shell", "command": "pwd", "cwd": str(outside_dir)},
                workspace_manager,
            )

            self.assertIn("error", result)
            self.assertEqual(result["cwd"], str(outside_dir))

    async def test_worker_shell_timeout_returns_diagnostics_and_logs_events(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-shell-timeout")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-shell-timeout",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Handle a shell timeout",
                workspace_id=child_workspace.id,
            )

            async def fake_run_command(self, request, on_event=None):
                del self, on_event
                return ShellCommandResult(
                    exit_code=-9,
                    stdout="partial output\n",
                    stderr="Command timed out after 10s and was force-terminated (process_group_killed).\n",
                    command=request.command,
                    timed_out=True,
                    duration_ms=10_042,
                    timeout_seconds=request.timeout_seconds,
                    killed=True,
                    termination_reason="process_group_killed",
                    reader_tasks_cancelled=True,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                result = await orchestrator._execute_shell_action(
                    agent,
                    {"type": "shell", "command": "sleep 999"},
                    workspace_manager,
                )

            self.assertTrue(result["timed_out"])
            self.assertTrue(result["killed"])
            self.assertTrue(result["reader_tasks_cancelled"])
            self.assertEqual(result["termination_reason"], "process_group_killed")

            diagnostics = orchestrator.diagnostics.read(session_id=agent.session_id)
            timeout_diag = next(
                record
                for record in diagnostics
                if record["event_type"] == "shell_command_timed_out"
            )
            self.assertEqual(timeout_diag["agent_id"], agent.id)
            self.assertEqual(timeout_diag["payload"]["termination_reason"], "process_group_killed")

            events = Storage(project_dir / ".opencompany" / "opencompany.db").load_events(
                agent.session_id
            )
            shell_timeout = next(
                event
                for event in events
                if event["event_type"] == "shell_timeout"
            )
            shell_timeout_payload = json.loads(shell_timeout["payload_json"])
            self.assertEqual(shell_timeout_payload["result"]["termination_reason"], "process_group_killed")

    async def test_worker_shell_permission_denied_logs_sandbox_violation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-shell-permission")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-shell-permission",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Handle permission denied",
                workspace_id=child_workspace.id,
            )

            async def fake_run_command(self, request, on_event=None):
                del self, on_event
                return ShellCommandResult(
                    exit_code=1,
                    stdout="",
                    stderr="cp: /tmp/outside.txt: Operation not permitted\n",
                    command=request.command,
                )

            with mock.patch(
                "opencompany.orchestrator.AnthropicSandboxBackend.run_command",
                new=fake_run_command,
            ):
                result = await orchestrator._execute_shell_action(
                    agent,
                    {"type": "shell", "command": "cp demo.txt /tmp/outside.txt"},
                    workspace_manager,
                )

            self.assertEqual(result["exit_code"], 1)

            events = Storage(project_dir / ".opencompany" / "opencompany.db").load_events(
                agent.session_id
            )
            violation_event = next(
                event
                for event in events
                if event["event_type"] == "sandbox_violation" and event["phase"] == "shell"
            )
            violation_payload = json.loads(violation_event["payload_json"])
            self.assertEqual(violation_payload["command"], "cp demo.txt /tmp/outside.txt")
            self.assertIn("outside the sandbox", violation_payload["error"])
            self.assertIn("Operation not permitted", violation_payload["stderr"])

    async def test_read_only_tool_timeout_budget_exceeded_logs_warning(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.tool_timeouts.actions["list_agent_runs"] = 0.01

            session_dir = orchestrator.paths.session_dir("session-read-timeout")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-read-timeout",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Trigger list timeout",
                workspace_id=child_workspace.id,
            )

            with mock.patch.object(
                orchestrator,
                "_execute_read_only_action",
                side_effect=lambda **kwargs: (time.sleep(0.05), {"agent_runs_count": 0, "agent_runs": []})[1],
            ):
                result = await orchestrator._execute_read_only_action_with_timeout(
                    agent=agent,
                    action={"type": "list_agent_runs"},
                    agents={agent.id: agent},
                    workspace_manager=workspace_manager,
                )

            self.assertTrue(result["timeout_budget_exceeded"])
            self.assertIn("exceeded timeout budget", result["warning"])
            self.assertEqual(result["agent_runs_count"], 0)

            diagnostics = orchestrator.diagnostics.read(session_id=agent.session_id)
            timeout_diag = next(
                record
                for record in diagnostics
                if record["event_type"] == "tool_action_timeout_budget_exceeded"
            )
            self.assertEqual(timeout_diag["payload"]["action_type"], "list_agent_runs")

            events = Storage(project_dir / ".opencompany" / "opencompany.db").load_events(
                agent.session_id
            )
            tool_timeout = next(
                event
                for event in events
                if event["event_type"] == "tool_timeout"
            )
            tool_timeout_payload = json.loads(tool_timeout["payload_json"])
            self.assertEqual(tool_timeout_payload["action"]["type"], "list_agent_runs")

    async def test_spawn_agent_timeout_budget_exceeded_is_reported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.tool_timeouts.actions["spawn_agent"] = 0.01

            session_dir = orchestrator.paths.session_dir("session-spawn-timeout")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            parent = AgentNode(
                id="agent-root",
                session_id="session-spawn-timeout",
                name="Root",
                role=AgentRole.ROOT,
                instruction="Trigger spawn timeout",
                workspace_id=root_workspace.id,
            )
            agents = {parent.id: parent}

            with mock.patch.object(
                orchestrator,
                "_spawn_child",
                side_effect=lambda **kwargs: (time.sleep(0.05), "agent-late")[1],
            ):
                child_id, tool_result = await orchestrator._spawn_child_with_timeout(
                    parent=parent,
                    action={
                        "type": "spawn_agent",
                        "name": "Late Worker",
                        "instruction": "Work",
                    },
                    agents=agents,
                    workspace_manager=workspace_manager,
                )

            self.assertEqual(child_id, "agent-late")
            self.assertTrue(tool_result["timeout_budget_exceeded"])
            self.assertIn("spawn_agent", tool_result["warning"])

            diagnostics = orchestrator.diagnostics.read(session_id=parent.session_id)
            timeout_diag = next(
                record
                for record in diagnostics
                if record["event_type"] == "tool_action_timeout_budget_exceeded"
            )
            self.assertEqual(timeout_diag["payload"]["action_type"], "spawn_agent")

            events = Storage(project_dir / ".opencompany" / "opencompany.db").load_events(
                parent.session_id
            )
            tool_timeout = next(
                event
                for event in events
                if event["event_type"] == "tool_timeout"
            )
            tool_timeout_payload = json.loads(tool_timeout["payload_json"])
            self.assertEqual(tool_timeout_payload["action"]["type"], "spawn_agent")

    async def test_root_cycle_stops_processing_actions_after_interrupt_request(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-root-interrupt")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            session = orchestrator._session_from_state(
                {
                    "id": "session-root-interrupt",
                    "project_dir": str(project_dir),
                    "task": "Inspect repository",
                    "locale": "en",
                    "root_agent_id": "agent-root",
                    "status": "running",
                    "created_at": "2026-03-09T00:00:00+00:00",
                    "updated_at": "2026-03-09T00:00:00+00:00",
                    "loop_index": 0,
                    "final_summary": None,
                    "completion_state": None,
                    "config_snapshot": {},
                }
            )
            root_agent = AgentNode(
                id="agent-root",
                session_id=session.id,
                name="Root Coordinator",
                role=AgentRole.ROOT,
                instruction="Inspect repository",
                workspace_id=root_workspace.id,
            )
            agents = {root_agent.id: root_agent}

            async def ask_and_interrupt(agent: AgentNode) -> list[dict[str, object]]:
                del agent
                orchestrator.interrupt_requested = True
                return [
                    {
                        "type": "finish",
                        "status": "completed",
                        "summary": "Should not finalize",
                    }
                ]

            orchestrator._ask_agent = ask_and_interrupt  # type: ignore[method-assign]
            pending = await orchestrator._run_root_cycle(
                session=session,
                root_agent=root_agent,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )

            self.assertEqual(pending, [])
            self.assertEqual(session.status.value, "running")
            self.assertIsNone(session.final_summary)

    def test_worker_read_file_alias_is_unknown_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_dir = root / "project"
            outside_file = root / "outside.txt"
            project_dir.mkdir()
            outside_file.write_text("secret\n", encoding="utf-8")
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-read-reject")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-read-reject",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Deprecated read_file should be rejected",
                workspace_id=child_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={"type": "read_file", "path": str(outside_file)},
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertIn("error", result)
            self.assertIn("not available", result["error"])
            self.assertEqual(result.get("error_code"), "unknown_tool")

    def test_search_text_alias_is_unknown_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-search-missing")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-search-missing",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Deprecated search_text should be rejected",
                workspace_id=child_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={"type": "search_text"},
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertIn("error", result)
            self.assertIn("not available", result["error"])
            self.assertEqual(result.get("error_code"), "unknown_tool")
            self.assertIn("available_tools", result)
            self.assertIn("shell", result["available_tools"])
            self.assertNotIn("search_text", result["available_tools"])

    def test_read_file_alias_returns_unknown_tool_feedback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-read-directory")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-read-directory",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Read a directory path",
                workspace_id=child_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={"type": "read_file", "path": "."},
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertIn("error", result)
            self.assertIn("not available", result["error"])
            self.assertEqual(result.get("error_code"), "unknown_tool")
            self.assertIn("next_step_hint", result)
            self.assertIn("available tools", result["next_step_hint"])

    def test_unknown_tool_returns_helpful_feedback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-unknown-tool")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-unknown-tool",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Handle unknown tool",
                workspace_id=child_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={
                    "type": "write_file",
                    "path": "demo.txt",
                    "content": "x" * 500,
                },
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertEqual(result.get("error_code"), "unknown_tool")
            self.assertIn("available_tools", result)
            self.assertIn("shell", result["available_tools"])
            self.assertIn("write_file", result["error"])
            self.assertIn("next_step_hint", result)
            self.assertIn("does not expose write_file", result["next_step_hint"])
            self.assertLessEqual(len(result["action"]["content"]), 200)

    def test_cancel_agent_without_agent_id_returns_tool_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-terminate-missing")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            agent = AgentNode(
                id="agent-root",
                session_id="session-terminate-missing",
                name="Root",
                role=AgentRole.ROOT,
                instruction="Reject invalid cancel_agent action",
                workspace_id=root_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={"type": "cancel_agent"},
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertIn("error", result)
            self.assertIn("requires 'agent_id'", result["error"])

    def test_spawn_child_without_instruction_is_skipped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-spawn-missing")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            parent = AgentNode(
                id="agent-root",
                session_id="session-spawn-missing",
                name="Root",
                role=AgentRole.ROOT,
                instruction="Reject invalid spawn_agent action",
                workspace_id=root_workspace.id,
            )
            agents = {parent.id: parent}

            child_id = orchestrator._spawn_child(
                parent=parent,
                action={"type": "spawn_agent", "name": "Child"},
                agents=agents,
                workspace_manager=workspace_manager,
            )

            self.assertIsNone(child_id)
            self.assertEqual(parent.children, [])
            self.assertEqual(list(agents), [parent.id])

    async def test_spawn_child_skip_warning_explains_total_child_limit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-spawn-limit")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            parent = AgentNode(
                id="agent-root",
                session_id="session-spawn-limit",
                name="Root",
                role=AgentRole.ROOT,
                instruction="Reject spawn_agent after total child limit",
                workspace_id=root_workspace.id,
                children=["agent-one", "agent-two", "agent-three"],
            )
            agents = {
                parent.id: parent,
                "agent-one": AgentNode(
                    id="agent-one",
                    session_id=parent.session_id,
                    name="One",
                    role=AgentRole.WORKER,
                    instruction="done",
                    workspace_id=root_workspace.id,
                    parent_agent_id=parent.id,
                    status=AgentStatus.COMPLETED,
                ),
                "agent-two": AgentNode(
                    id="agent-two",
                    session_id=parent.session_id,
                    name="Two",
                    role=AgentRole.WORKER,
                    instruction="stopped",
                    workspace_id=root_workspace.id,
                    parent_agent_id=parent.id,
                    status=AgentStatus.TERMINATED,
                ),
                "agent-three": AgentNode(
                    id="agent-three",
                    session_id=parent.session_id,
                    name="Three",
                    role=AgentRole.WORKER,
                    instruction="failed",
                    workspace_id=root_workspace.id,
                    parent_agent_id=parent.id,
                    status=AgentStatus.FAILED,
                ),
            }

            child_id, result = await orchestrator._spawn_child_with_timeout(
                parent=parent,
                action={
                    "type": "spawn_agent",
                    "name": "Child Four",
                    "instruction": "Try to create one more child",
                },
                agents=agents,
                workspace_manager=workspace_manager,
            )

            self.assertIsNone(child_id)
            self.assertEqual(result["reason"], "max_children_per_agent")
            self.assertEqual(result["limit_scope"], "total_children")
            self.assertEqual(result["current_children"], 3)
            self.assertEqual(result["active_children"], 0)
            self.assertIn("Terminal children still count", result["warning"])

            diagnostics_path = project_dir / ".opencompany" / "diagnostics.jsonl"
            diagnostic_lines = diagnostics_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(diagnostic_lines)
            payload = json.loads(diagnostic_lines[-1])["payload"]
            self.assertEqual(payload["reason"], "max_children_per_agent")
            self.assertEqual(payload["limit_scope"], "total_children")
            self.assertEqual(payload["current_children"], 3)
            self.assertEqual(payload["active_children"], 0)

    async def test_spawn_child_skip_result_uses_post_failure_limit_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.config.runtime.limits.max_children_per_agent = 1

            session_dir = orchestrator.paths.session_dir("session-spawn-limit-post-failure")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            parent = AgentNode(
                id="agent-root",
                session_id="session-spawn-limit-post-failure",
                name="Root",
                role=AgentRole.ROOT,
                instruction="Return a clear limit warning",
                workspace_id=root_workspace.id,
            )
            agents = {parent.id: parent}

            def fake_spawn_child(*args: object, **kwargs: object) -> str | None:
                del args, kwargs
                child = AgentNode(
                    id="agent-race-child",
                    session_id=parent.session_id,
                    name="Race Child",
                    role=AgentRole.WORKER,
                    instruction="noop",
                    workspace_id=root_workspace.id,
                    parent_agent_id=parent.id,
                    status=AgentStatus.RUNNING,
                )
                parent.children.append(child.id)
                agents[child.id] = child
                return None

            with mock.patch.object(orchestrator, "_spawn_child", side_effect=fake_spawn_child):
                child_id, result = await orchestrator._spawn_child_with_timeout(
                    parent=parent,
                    action={
                        "type": "spawn_agent",
                        "name": "Will Be Skipped",
                        "instruction": "Try to spawn one more child",
                    },
                    agents=agents,
                    workspace_manager=workspace_manager,
                )

            self.assertIsNone(child_id)
            self.assertEqual(result["reason"], "max_children_per_agent")
            self.assertEqual(result["limit_scope"], "total_children")
            self.assertEqual(result["current_children"], 1)
            self.assertEqual(result["active_children"], 1)
            self.assertIn("total child fan-out limit", result["warning"])

    def test_worker_read_file_binary_alias_is_unknown_tool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            (project_dir / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0binary")
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            session_dir = orchestrator.paths.session_dir("session-binary-read")
            workspace_manager = WorkspaceManager(session_dir)
            root_workspace = workspace_manager.create_root_workspace(project_dir)
            child_workspace = workspace_manager.fork_workspace(root_workspace.id, "agent-child")
            agent = AgentNode(
                id="agent-child",
                session_id="session-binary-read",
                name="Worker",
                role=AgentRole.WORKER,
                instruction="Deprecated read_file should be rejected",
                workspace_id=child_workspace.id,
            )

            result = orchestrator._execute_read_only_action(
                agent=agent,
                action={"type": "read_file", "path": "sample.doc"},
                agents={agent.id: agent},
                workspace_manager=workspace_manager,
            )

            self.assertIn("error", result)
            self.assertIn("not available", result["error"])
            self.assertEqual(result.get("error_code"), "unknown_tool")

    async def test_worker_search_text_alias_does_not_fail_session(self) -> None:
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
                                        "type": "search_text",
                                        "text": "demo",
                                    },
                                    {
                                        "type": "finish",
                                        "status": "completed",
                                        "summary": "Inspection finished",
                                        "next_recommendation": "Finalize",
                                    },
                                ]
                            }
                        )
                    ],
                }
            )

            session = await orchestrator.run_task("Inspect this project")

            self.assertEqual(session.status.value, "completed")
            self.assertEqual(session.final_summary, "All work completed.")

    def test_child_summaries_do_not_expose_diff_artifact_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)

            parent = AgentNode(
                id="agent-parent",
                session_id="session-child-summary",
                name="Parent",
                role=AgentRole.WORKER,
                instruction="Collect child output",
                workspace_id="ws-parent",
                children=["agent-child"],
            )
            child = AgentNode(
                id="agent-child",
                session_id="session-child-summary",
                name="Child",
                role=AgentRole.WORKER,
                instruction="Read a file",
                workspace_id="ws-child",
                parent_agent_id=parent.id,
                status=AgentStatus.COMPLETED,
                summary="Read the file.",
                next_recommendation="Use the summary.",
                diff_artifact="/tmp/agent-child.json",
            )

            summaries = orchestrator._child_summaries(
                parent,
                {parent.id: parent, child.id: child},
            )

            self.assertEqual(len(summaries), 1)
            self.assertNotIn("diff_artifact", summaries[0])

    async def test_runtime_events_include_stream_and_tool_activity(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "list_agent_runs",
                                },
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Listed runs.",
                                },
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task("List agent runs")
            self.assertEqual(session.status.value, "completed")

            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            events = storage.load_events(session.id)
            event_types = [event["event_type"] for event in events]
            self.assertIn("session_started", event_types)
            self.assertIn("agent_prompt", event_types)
            self.assertIn("llm_token", event_types)
            self.assertIn("tool_call_started", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("session_finalized", event_types)

            session_started = next(event for event in events if event["event_type"] == "session_started")
            session_payload = json.loads(session_started["payload_json"])
            self.assertEqual(session_payload["root_agent_name"], "Root Coordinator")

            prompt_event = next(event for event in events if event["event_type"] == "agent_prompt")
            prompt_payload = json.loads(prompt_event["payload_json"])
            self.assertIn("system_prompt", prompt_payload)
            self.assertIn("tools", prompt_payload)
            self.assertTrue(prompt_payload["tools"])
            self.assertNotIn("Target project directory:", prompt_payload["messages"][0]["content"])
            self.assertIn("You are the root coordinator", prompt_payload["messages"][0]["content"])

            tool_call = next(
                event
                for event in events
                if event["event_type"] == "tool_call" and event["phase"] == "tool"
            )
            tool_payload = json.loads(tool_call["payload_json"])
            self.assertEqual(tool_payload["action"]["type"], "list_agent_runs")

    async def test_failed_run_is_persisted_as_failed_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = None

            with self.assertRaises(RuntimeError):
                await orchestrator.run_task("This should fail without an API key")

            assert orchestrator.latest_session_id is not None
            storage = Storage(project_dir / ".opencompany" / "opencompany.db")
            session_row = storage.load_session(orchestrator.latest_session_id)
            assert session_row is not None
            self.assertEqual(session_row["status"], "failed")
            events = storage.load_events(orchestrator.latest_session_id)
            self.assertTrue(any(event["event_type"] == "session_failed" for event in events))

    async def test_export_includes_session_diagnostics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Finished with diagnostics.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task("Finalize with diagnostics")
            export_path = orchestrator.export_logs(session.id)
            exported = json.loads(export_path.read_text(encoding="utf-8"))

            self.assertIn("diagnostics", exported)
            self.assertIn("agent_messages", exported)
            self.assertIn("tool_run_metrics", exported)
            self.assertIn("steer_run_metrics", exported)
            self.assertIn(session.root_agent_id, exported["agent_messages"])
            self.assertTrue(exported["diagnostics"])
            self.assertGreaterEqual(int(exported["tool_run_metrics"].get("total_runs", 0)), 1)
            self.assertGreaterEqual(int(exported["steer_run_metrics"].get("total_runs", 0)), 0)
            self.assertTrue(
                any(
                    record["event_type"] == "session_finalized"
                    for record in exported["diagnostics"]
                )
            )

            metrics_export_path = orchestrator.export_tool_run_metrics(session.id)
            exported_metrics = json.loads(metrics_export_path.read_text(encoding="utf-8"))
            self.assertEqual(exported_metrics.get("session_id"), session.id)
            self.assertGreaterEqual(int(exported_metrics.get("total_runs", 0)), 1)

    async def test_export_logs_accepts_custom_export_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Finished with custom export path.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task("Finalize and export logs")
            custom_export_path = project_dir / "tmp" / "exports" / "session-export.json"
            export_path = orchestrator.export_logs(session.id, export_path=custom_export_path)
            exported = json.loads(custom_export_path.read_text(encoding="utf-8"))

            self.assertTrue(export_path.samefile(custom_export_path))
            self.assertTrue(custom_export_path.exists())
            self.assertEqual(exported.get("session", {}).get("id"), session.id)

    async def test_export_tool_run_metrics_accepts_custom_export_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            build_test_project(project_dir)
            orchestrator = Orchestrator(project_dir, locale="en", app_dir=project_dir)
            orchestrator.llm_client = FakeLLMClient(
                [
                    json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "Finished for tool run metrics export.",
                                }
                            ]
                        }
                    )
                ]
            )

            session = await orchestrator.run_task("Finalize and export tool run metrics")
            custom_export_path = project_dir / "tmp" / "exports" / "tool-run-metrics.json"
            export_path = orchestrator.export_tool_run_metrics(
                session.id,
                export_path=custom_export_path,
            )
            exported_metrics = json.loads(custom_export_path.read_text(encoding="utf-8"))

            self.assertTrue(export_path.samefile(custom_export_path))
            self.assertTrue(custom_export_path.exists())
            self.assertEqual(exported_metrics.get("session_id"), session.id)
            self.assertGreaterEqual(int(exported_metrics.get("total_runs", 0)), 1)

    def test_open_session_terminal_uses_sandbox_command_and_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)

            captured: dict[str, object] = {}

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    captured["workspace_root"] = str(request.workspace_root.resolve())
                    captured["writable_paths"] = [str(path.resolve()) for path in request.writable_paths]
                    return {
                        "filesystem": {
                            "allowWrite": [
                                str(request.workspace_root.resolve()),
                                "/tmp",
                            ]
                        }
                    }

                def build_sandbox_command(self, command: str) -> str:
                    return f"/bin/bash -lc {command}"

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]
            with mock.patch.object(
                orchestrator,
                "_open_system_terminal",
                side_effect=lambda command: captured.setdefault("command", command),
            ):
                opened = orchestrator.open_session_terminal("session-1")

            self.assertEqual(opened["workspace_root"], str(workspace_root))
            self.assertEqual(captured["workspace_root"], str(workspace_root))
            self.assertEqual(captured["writable_paths"], [str(workspace_root)])

            settings_path = Path(str(opened["settings_path"]))
            self.assertTrue(settings_path.exists())
            settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
            allow_write = (
                settings_payload.get("filesystem", {}).get("allowWrite", [])
                if isinstance(settings_payload, dict)
                else []
            )
            self.assertIn(str(workspace_root), allow_write)
            self.assertIn("/tmp", allow_write)

            launch_command = str(captured["command"])
            self.assertIn("&& exec ", launch_command)
            self.assertIn("--settings", launch_command)
            self.assertIn(str(workspace_root), launch_command)
            self.assertIn("/bin/bash --noprofile --norc -i", launch_command)
            self.assertNotIn("--norc -c 'exec /bin/bash", launch_command)
            self.assertEqual(settings_path.name, "settings.json")

    def test_open_session_terminal_reuses_settings_file_and_cleans_legacy_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            terminal_dir = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "terminal"
            ).resolve()
            terminal_dir.mkdir(parents=True, exist_ok=True)
            legacy_path = terminal_dir / "settings_123_old.json"
            legacy_path.write_text("{}", encoding="utf-8")
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {"filesystem": {"allowWrite": [str(request.workspace_root.resolve())]}}

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]
            with mock.patch.object(orchestrator, "_open_system_terminal", return_value=None):
                first = orchestrator.open_session_terminal("session-1")
                second = orchestrator.open_session_terminal("session-1")

            self.assertEqual(first["settings_path"], second["settings_path"])
            settings_path = Path(first["settings_path"])
            self.assertTrue(settings_path.exists())
            self.assertFalse(legacy_path.exists())

    def test_open_session_terminal_none_backend_uses_plain_bash_without_srt_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "none"
""".strip(),
                encoding="utf-8",
            )
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            captured: dict[str, object] = {}

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            with mock.patch.object(
                orchestrator,
                "_open_system_terminal",
                side_effect=lambda command: captured.setdefault("command", command),
            ):
                opened = orchestrator.open_session_terminal("session-1")

            launch_command = str(captured["command"])
            self.assertEqual(opened["workspace_root"], str(workspace_root))
            self.assertIn("/bin/bash --noprofile --norc -i", launch_command)
            self.assertNotIn("srt --settings", launch_command)
            self.assertNotIn("--settings", launch_command)

    def test_open_session_terminal_remote_none_backend_does_not_use_srt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "none"
""".strip(),
                encoding="utf-8",
            )
            session_dir = (app_dir / ".opencompany" / "sessions" / "session-1").resolve()
            workspace_root = (session_dir / "snapshots" / "root").resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            (session_dir / "remote_session.json").write_text(
                json.dumps(
                    {
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:33885",
                        "remote_dir": str(workspace_root),
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            with mock.patch.object(
                orchestrator,
                "_open_system_terminal",
                side_effect=lambda command: captured.setdefault("command", command),
            ):
                orchestrator.open_session_terminal("session-1")

            launch_command = str(captured["command"])
            self.assertIn("-p 33885", launch_command)
            self.assertIn("/bin/bash --noprofile --norc -i", launch_command)
            self.assertNotIn("srt --settings", launch_command)
            self.assertNotIn("[opencompany][remote-setup]", launch_command)

    def test_open_session_terminal_remote_uses_explicit_ssh_port(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = (app_dir / ".opencompany" / "sessions" / "session-1").resolve()
            workspace_root = (session_dir / "snapshots" / "root").resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            (session_dir / "remote_session.json").write_text(
                json.dumps(
                    {
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:33885",
                        "remote_dir": str(workspace_root),
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {"filesystem": {"allowWrite": [str(request.workspace_root.resolve())]}}

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]
            with mock.patch.object(
                orchestrator,
                "_open_system_terminal",
                side_effect=lambda command: captured.setdefault("command", command),
            ):
                orchestrator.open_session_terminal("session-1")

            launch_command = str(captured["command"])
            self.assertIn("-p 33885", launch_command)
            self.assertIn("demo@example.com", launch_command)
            self.assertNotIn("demo@example.com:33885", launch_command)
            self.assertIn("ControlPath=/tmp/opencompany-ssh/", launch_command)
            self.assertNotIn(".opencompany/remote_runtime", launch_command)
            self.assertIn("[opencompany][remote-setup]", launch_command)
            self.assertIn("resolved_workspace_root", launch_command)
            self.assertIn("/bin/pwd -P", launch_command)

    def test_open_session_terminal_remote_password_auth_uses_sshpass_without_prompt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = (app_dir / ".opencompany" / "sessions" / "session-1").resolve()
            workspace_root = (session_dir / "snapshots" / "root").resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            (session_dir / "remote_session.json").write_text(
                json.dumps(
                    {
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:22",
                        "remote_dir": str(workspace_root),
                        "auth_mode": "password",
                        "identity_file": "",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {"filesystem": {"allowWrite": [str(request.workspace_root.resolve())]}}

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]
            with mock.patch(
                "opencompany.orchestrator.shutil.which",
                return_value="/usr/local/bin/sshpass",
            ), mock.patch.object(
                orchestrator,
                "_open_system_terminal",
                side_effect=lambda command: captured.setdefault("command", command),
            ):
                orchestrator.open_session_terminal("session-1", remote_password="secret-pass")

            launch_command = str(captured["command"])
            self.assertIn("/usr/local/bin/sshpass -f", launch_command)
            self.assertIn("trap cleanup EXIT INT TERM", launch_command)
            self.assertNotIn("secret-pass", launch_command)

    def test_resolve_session_workspace_path_remote_preserves_posix_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_dir = (app_dir / ".opencompany" / "sessions" / "session-remote").resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "remote_session.json").write_text(
                json.dumps(
                    {
                        "kind": "remote_ssh",
                        "ssh_target": "demo@example.com:22",
                        "remote_dir": "/home/ubuntu/test",
                        "auth_mode": "key",
                        "identity_file": "~/.ssh/id_ed25519",
                        "known_hosts_policy": "accept_new",
                        "remote_os": "linux",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)

            workspace = orchestrator.resolve_session_workspace_path("session-remote")

            self.assertEqual(str(workspace), "/home/ubuntu/test")

    def test_build_shell_request_remote_preserves_posix_workspace_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            remote = RemoteShellContext(
                session_id="session-remote",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/ubuntu/test",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            request = orchestrator.tool_executor.build_shell_request(
                workspace_root=Path("/home/ubuntu/test"),
                command="pwd",
                cwd=".",
                writable_paths=[Path("/home/ubuntu/test")],
                session_id="session-remote",
                remote=remote,
            )

            self.assertEqual(str(request.workspace_root), "/home/ubuntu/test")
            self.assertEqual(str(request.cwd), "/home/ubuntu/test")
            self.assertEqual([str(path) for path in request.writable_paths], ["/home/ubuntu/test"])

    def test_normalize_workspace_state_direct_root_uses_project_dir_verbatim(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            session_id = "session-remote"
            session_dir = (app_dir / ".opencompany" / "sessions" / session_id).resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)

            normalized = orchestrator._normalize_workspace_state_for_session(
                session_id=session_id,
                workspace_mode="direct",
                project_dir=Path("/home/ubuntu/test"),
                workspaces_state={
                    "root": {
                        "id": "root",
                        "path": "/System/Volumes/Data/home/ubuntu/test",
                        "base_snapshot_path": "/System/Volumes/Data/home/ubuntu/test",
                        "readonly": False,
                    }
                },
            )

            self.assertEqual(normalized["root"]["path"], "/home/ubuntu/test")
            self.assertEqual(normalized["root"]["base_snapshot_path"], "/home/ubuntu/test")

    def test_append_shell_stream_output_filters_remote_setup_status_lines(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            run_id = "toolrun-remote-setup-filter"

            orchestrator._append_shell_stream_output(
                tool_run_id=run_id,
                channel="stderr",
                text=(
                    "[opencompany][remote-setup] Checking remote sandbox dependencies\n"
                    "real stderr line 1\n"
                    "[opencompany][remote-setup] Remote sandbox dependencies ready\n"
                    "real stderr line 2\n"
                ),
            )
            snapshot = orchestrator._shell_stream_snapshot(run_id)

            self.assertNotIn("[opencompany][remote-setup]", snapshot["stderr"])
            self.assertIn("real stderr line 1", snapshot["stderr"])
            self.assertIn("real stderr line 2", snapshot["stderr"])

    def test_open_system_terminal_on_darwin_uses_open_with_script_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            captured: dict[str, object] = {}

            def _fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
                captured["args"] = list(args)
                return mock.Mock(returncode=0, stderr="", stdout="")

            with mock.patch("opencompany.orchestrator.sys.platform", "darwin"):
                with mock.patch("opencompany.orchestrator.subprocess.run", side_effect=_fake_run):
                    orchestrator._open_system_terminal("echo hello")

            args = captured.get("args")
            self.assertIsInstance(args, list)
            assert isinstance(args, list)
            self.assertGreaterEqual(len(args), 4)
            self.assertEqual(args[0], "open")
            self.assertEqual(args[1], "-a")
            self.assertEqual(args[2], "Terminal")
            script_path = Path(str(args[3]))
            self.assertTrue(script_path.exists())
            script_text = script_path.read_text(encoding="utf-8")
            self.assertIn("echo hello", script_text)

    def test_open_system_terminal_on_darwin_cleans_legacy_random_scripts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            data_dir = app_dir / ".opencompany"
            launch_dir = data_dir / "terminal_launch"
            launch_dir.mkdir(parents=True, exist_ok=True)
            legacy = launch_dir / "launch_1773304853_6664aff2.command"
            legacy.write_text("#!/bin/bash\necho legacy\n", encoding="utf-8")
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)

            with mock.patch("opencompany.orchestrator.sys.platform", "darwin"):
                with mock.patch(
                    "opencompany.orchestrator.subprocess.run",
                    return_value=mock.Mock(returncode=0, stderr="", stdout=""),
                ):
                    orchestrator._open_system_terminal("echo hello")

            self.assertFalse(legacy.exists())

    async def test_terminal_self_check_passes_when_outside_write_is_blocked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            terminal_dir = (app_dir / ".opencompany" / "sessions" / "session-1" / "terminal").resolve()
            terminal_dir.mkdir(parents=True, exist_ok=True)

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {
                        "filesystem": {
                            "allowWrite": [
                                str(request.workspace_root.resolve()),
                                "/tmp",
                            ]
                        },
                        "network": {"allowedDomains": []},
                    }

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

                async def run_command(self, request, on_event=None):  # type: ignore[no-untyped-def]
                    command = str(request.command)
                    if ".terminal_self_check_" in command:
                        return ShellCommandResult(
                            exit_code=0,
                            stdout="ok",
                            stderr="",
                            command=command,
                        )
                    return ShellCommandResult(
                        exit_code=1,
                        stdout="",
                        stderr="permission denied",
                        command=command,
                    )

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]

            report = await orchestrator.terminal_self_check("session-1")

            self.assertTrue(report["passed"])
            checks = report["checks"]
            self.assertTrue(checks["policy_match_agent_shell"]["ok"])
            self.assertTrue(checks["settings_match_agent_shell"]["ok"])
            self.assertTrue(checks["workspace_write"]["ok"])
            self.assertTrue(checks["outside_write_blocked"]["ok"])
            self.assertTrue(checks["outside_write_policy_match"]["ok"])
            self.assertIsNone(report["runtime_error"])

    async def test_terminal_self_check_fails_when_outside_write_succeeds(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            terminal_dir = (app_dir / ".opencompany" / "sessions" / "session-1" / "terminal").resolve()
            terminal_dir.mkdir(parents=True, exist_ok=True)

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {
                        "filesystem": {
                            "allowWrite": [
                                str(request.workspace_root.resolve()),
                                "/tmp",
                            ]
                        },
                        "network": {"allowedDomains": []},
                    }

                def resolve_cli_path(self) -> str:
                    return "/usr/local/bin/srt"

                async def run_command(self, request, on_event=None):  # type: ignore[no-untyped-def]
                    command = str(request.command)
                    if ".terminal_self_check_" in command:
                        return ShellCommandResult(
                            exit_code=0,
                            stdout="ok",
                            stderr="",
                            command=command,
                        )
                    marker = "terminal_escape_"
                    if marker in command:
                        suffix = command.split(marker, 1)[1].split(".tmp", 1)[0]
                        outside_path = terminal_dir / f"{marker}{suffix}.tmp"
                        outside_path.write_text("leaked", encoding="utf-8")
                    return ShellCommandResult(
                        exit_code=0,
                        stdout="",
                        stderr="",
                        command=command,
                    )

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]

            report = await orchestrator.terminal_self_check("session-1")

            self.assertFalse(report["passed"])
            self.assertFalse(report["checks"]["outside_write_blocked"]["ok"])
            self.assertFalse(report["checks"]["outside_write_policy_match"]["ok"])
            self.assertTrue(report["checks"]["outside_write_blocked"]["exists_after"])

    async def test_terminal_self_check_reports_runtime_error_when_workspace_write_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text("", encoding="utf-8")
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)

            class _FakeBackend:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def build_settings(self, request):  # type: ignore[no-untyped-def]
                    return {
                        "filesystem": {
                            "allowWrite": [
                                str(request.workspace_root.resolve()),
                                "/tmp",
                            ]
                        },
                        "network": {"allowedDomains": []},
                    }

                async def run_command(self, request, on_event=None):  # type: ignore[no-untyped-def]
                    return ShellCommandResult(
                        exit_code=1,
                        stdout="",
                        stderr="sandbox runtime failed to start",
                        command=str(request.command),
                    )

            orchestrator = Orchestrator(Path("."), app_dir=app_dir)
            orchestrator.tool_executor.sandbox_backend_cls = _FakeBackend  # type: ignore[assignment]

            report = await orchestrator.terminal_self_check("session-1")

            self.assertFalse(report["passed"])
            self.assertFalse(report["checks"]["workspace_write"]["ok"])
            self.assertFalse(report["checks"]["outside_write_blocked"]["ok"])
            self.assertIn("sandbox runtime failed to start", str(report["runtime_error"]))

    async def test_terminal_self_check_none_backend_accepts_outside_write(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "none"
""".strip(),
                encoding="utf-8",
            )
            workspace_root = (
                app_dir / ".opencompany" / "sessions" / "session-1" / "snapshots" / "root"
            ).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            terminal_dir = (app_dir / ".opencompany" / "sessions" / "session-1" / "terminal").resolve()
            terminal_dir.mkdir(parents=True, exist_ok=True)
            orchestrator = Orchestrator(Path("."), app_dir=app_dir)

            report = await orchestrator.terminal_self_check("session-1")

            self.assertTrue(report["passed"])
            self.assertFalse(report["outside_write_expected_blocked"])
            self.assertTrue(report["checks"]["workspace_write"]["ok"])
            self.assertFalse(report["checks"]["outside_write_blocked"]["ok"])
            self.assertTrue(report["checks"]["outside_write_policy_match"]["ok"])
