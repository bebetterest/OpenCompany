from __future__ import annotations

import json
import unittest
from typing import Any

from opencompany.config import OpenCompanyConfig
from opencompany.llm.openrouter import ChatResult
from opencompany.models import AgentNode, AgentRole
from opencompany.orchestration.agent_runtime import (
    AgentRuntime,
    usage_cache_read_tokens,
    usage_cache_write_tokens,
    usage_output_tokens,
    usage_total_tokens,
)
from opencompany.orchestration.context import ContextAssembler
from opencompany.prompts import PromptLibrary, default_prompts_dir


class RecordingLLMClient:
    def __init__(self, responses: list[ChatResult]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def stream_chat(self, **kwargs: Any) -> ChatResult:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Unexpected extra LLM call.")
        return self._responses.pop(0)


class ContextAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = OpenCompanyConfig()
        self.config.runtime.context.keep_pinned_messages = 1
        self.prompt_library = PromptLibrary(default_prompts_dir())
        self.assembler = ContextAssembler(
            config=self.config,
            locale="en",
            prompt_library=self.prompt_library,
        )

    @staticmethod
    def _agent(conversation: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> AgentNode:
        return AgentNode(
            id="agent-root",
            session_id="session-1",
            name="Root",
            role=AgentRole.ROOT,
            instruction="test",
            workspace_id="workspace-root",
            conversation=conversation,
            metadata=metadata or {},
        )

    def test_messages_without_summary_include_system_plus_conversation(self) -> None:
        agent = self._agent(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ]
        )
        messages = self.assembler.messages(agent, "SYSTEM")
        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
        )

    def test_messages_with_summary_include_pinned_summary_and_unsummarized(self) -> None:
        agent = self._agent(
            [
                {"role": "user", "content": "head pinned"},
                {"role": "assistant", "content": "already summarized"},
                {"role": "assistant", "content": "compress tool call trace"},
                {"role": "user", "content": "new user message"},
                {"role": "tool", "content": "compress tool result"},
                {"role": "assistant", "content": "latest assistant reply"},
            ],
            metadata={
                "context_summary": "latest concise summary",
                "summary_version": 2,
                "summarized_until_message_index": 1,
                "internal_message_indices": [2, 4],
            },
        )
        messages = self.assembler.messages(agent, "SYSTEM")
        self.assertEqual(messages[0], {"role": "system", "content": "SYSTEM"})
        self.assertEqual(messages[1], {"role": "user", "content": "head pinned"})
        self.assertEqual(messages[2]["role"], "user")
        self.assertIn("compressed as follows (v2)", str(messages[2]["content"]))
        self.assertEqual(messages[3], {"role": "user", "content": "new user message"})
        self.assertEqual(messages[4], {"role": "assistant", "content": "latest assistant reply"})
        self.assertEqual(len(messages), 5)


class AgentRuntimeContextTests(unittest.IsolatedAsyncioTestCase):
    def _build_runtime(
        self,
        *,
        config: OpenCompanyConfig,
        event_sink: list[dict[str, Any]],
    ) -> AgentRuntime:
        prompt_library = PromptLibrary(default_prompts_dir())

        def persist_agent(_agent: AgentNode) -> None:
            return None

        def log_agent_event(
            agent: AgentNode,
            *,
            event_type: str,
            phase: str,
            payload: dict[str, Any],
        ) -> None:
            event_sink.append(
                {
                    "agent_id": agent.id,
                    "event_type": event_type,
                    "phase": phase,
                    "payload": payload,
                }
            )

        def append_agent_message(
            agent: AgentNode,
            message: dict[str, Any],
            _stored_message: dict[str, Any] | None,
            metadata: dict[str, Any] | None,
        ) -> None:
            agent.conversation.append(message)
            message_index = len(agent.conversation) - 1
            if not isinstance(agent.metadata, dict):
                agent.metadata = {}
            raw_step_map = agent.metadata.get("message_index_to_step")
            step_map = list(raw_step_map) if isinstance(raw_step_map, list) else []
            while len(step_map) < message_index:
                step_map.append(0)
            step_map.append(max(0, int(agent.step_count)))
            agent.metadata["message_index_to_step"] = step_map
            if metadata and bool(metadata.get("internal")):
                raw_indices = agent.metadata.get("internal_message_indices")
                normalized = list(raw_indices) if isinstance(raw_indices, list) else []
                normalized.append(message_index)
                deduped: set[int] = set()
                for value in normalized:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        deduped.add(index)
                agent.metadata["internal_message_indices"] = sorted(deduped)
            if metadata and bool(metadata.get("exclude_from_context_compression")):
                raw_indices = agent.metadata.get("compression_excluded_message_indices")
                normalized = list(raw_indices) if isinstance(raw_indices, list) else []
                normalized.append(message_index)
                deduped: set[int] = set()
                for value in normalized:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        deduped.add(index)
                agent.metadata["compression_excluded_message_indices"] = sorted(deduped)

        return AgentRuntime(
            config=config,
            locale="en",
            prompt_library=prompt_library,
            persist_agent=persist_agent,
            log_agent_event=log_agent_event,
            append_agent_message=append_agent_message,
        )

    @staticmethod
    def _root_agent() -> AgentNode:
        return AgentNode(
            id="agent-root",
            session_id="session-1",
            name="Root",
            role=AgentRole.ROOT,
            instruction="test task",
            workspace_id="workspace-root",
            conversation=[{"role": "user", "content": "task start"}],
            metadata={"message_index_to_step": [0]},
        )

    async def test_compress_context_requires_runtime_compression_model(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = ""
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient([])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertFalse(bool(result.get("compressed")))
        self.assertIn("compression_model", str(result.get("error", "")))

    def test_context_limit_uses_max_context_tokens_only(self) -> None:
        config = OpenCompanyConfig()
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)

        config.runtime.context.max_context_tokens = 7777
        self.assertEqual(runtime._resolved_context_limit_tokens("openai/gpt-4o-mini"), 7777)

    async def test_compress_context_updates_estimated_after_tokens(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        agent.metadata["current_context_tokens"] = 4096
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertTrue(bool(result.get("compressed")))
        after_tokens = int(result.get("context_tokens_after", 0))
        self.assertGreater(after_tokens, 0)
        self.assertEqual(int(agent.metadata.get("current_context_tokens", 0)), after_tokens)
        self.assertNotEqual(int(result.get("context_tokens_before", 0)), after_tokens)

    async def test_compress_context_uses_configured_timeout_seconds(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.tool_timeouts.actions["compress_context"] = 222
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertTrue(bool(result.get("compressed")))
        self.assertEqual(float(llm.calls[0].get("timeout_seconds", 0)), 222.0)

    async def test_compress_context_excludes_internal_compression_traces(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        agent.conversation.append({"role": "assistant", "content": "{\"type\":\"compress_context\"}"})
        agent.conversation.append({"role": "tool", "content": "{\"compressed\":true}"})
        agent.metadata["internal_message_indices"] = [1, 2]
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertTrue(bool(result.get("compressed")))
        summary_request = llm.calls[0]["messages"]
        assert isinstance(summary_request, list)
        summary_payload = json.loads(str(summary_request[1]["content"]))
        unsummarized = summary_payload.get("unsummarized_messages", [])
        self.assertEqual(len(unsummarized), 1)
        self.assertEqual(int(unsummarized[0].get("message_index", -1)), 0)
        self.assertEqual(str(unsummarized[0].get("content", "")), "task start")

    async def test_compress_context_excludes_compression_ignored_control_messages(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        agent.conversation.extend(
            [
                {"role": "user", "content": "root soft-step reminder"},
                {"role": "user", "content": "context pressure reminder"},
                {"role": "assistant", "content": "actual work update"},
            ]
        )
        agent.metadata["compression_excluded_message_indices"] = [1, 2]
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertTrue(bool(result.get("compressed")))
        summary_request = llm.calls[0]["messages"]
        assert isinstance(summary_request, list)
        summary_payload = json.loads(str(summary_request[1]["content"]))
        unsummarized = summary_payload.get("unsummarized_messages", [])
        self.assertEqual(
            [int(item.get("message_index", -1)) for item in unsummarized],
            [0, 3],
        )
        self.assertEqual(
            [str(item.get("content", "")) for item in unsummarized],
            ["task start", "actual work update"],
        )

    async def test_compress_context_advances_summarized_until_past_control_traces(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        agent.conversation.extend(
            [
                {"role": "assistant", "content": "actual work update"},
                {"role": "user", "content": "context pressure reminder"},
                {"role": "assistant", "content": "{\"type\":\"compress_context\"}"},
            ]
        )
        agent.metadata["message_index_to_step"] = [1, 2, 34, 34]
        agent.metadata["compression_excluded_message_indices"] = [2]
        agent.metadata["internal_message_indices"] = [3]
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )

        self.assertTrue(bool(result.get("compressed")))
        self.assertEqual(result.get("message_range"), {"start": 0, "end": 1})
        self.assertEqual(int(agent.metadata.get("summarized_until_message_index", -1)), 3)

        request_messages = runtime.context_assembler.messages(
            agent,
            runtime.context_assembler.system_prompt(agent),
        )
        serialized_request = json.dumps(request_messages, ensure_ascii=False)
        self.assertNotIn("context pressure reminder", serialized_request)
        self.assertNotIn("compress_context", serialized_request)

    def test_context_pressure_reminder_marks_message_for_compression_exclusion(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.max_context_tokens = 1000
        config.runtime.context.reminder_ratio = 0.6
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        agent.metadata["current_context_tokens"] = 700

        runtime._maybe_append_context_pressure_reminder(
            agent=agent,
            selected_model=config.llm.openrouter.model_for_role(agent.role.value),
        )

        self.assertEqual(str(agent.conversation[-1].get("role", "")), "user")
        self.assertIn(
            "Context usage warning",
            str(agent.conversation[-1].get("content", "")),
        )
        raw_indices = agent.metadata.get("compression_excluded_message_indices")
        self.assertIsInstance(raw_indices, list)
        assert isinstance(raw_indices, list)
        self.assertIn(len(agent.conversation) - 1, [int(value) for value in raw_indices])

    async def test_overflow_response_triggers_forced_compress_and_single_retry(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.context.overflow_retry_attempts = 1
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient(
            [
                ChatResult(
                    content="",
                    raw_events=[],
                    response_error={
                        "code": "context_length_exceeded",
                        "message": "context window exceeded",
                    },
                ),
                ChatResult(content="compressed summary body", raw_events=[]),
                ChatResult(
                    content=json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "done",
                                }
                            ]
                        }
                    ),
                    raw_events=[],
                    usage={"prompt_tokens": 1234},
                ),
            ]
        )

        actions = await runtime.ask(agent, llm_client=llm)

        self.assertEqual(actions[0].get("type"), "finish")
        self.assertEqual(actions[0].get("status"), "completed")
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(str(llm.calls[1].get("model", "")), "compress/model")

        summary_request = llm.calls[1]["messages"]
        assert isinstance(summary_request, list)
        summary_payload = json.loads(str(summary_request[1]["content"]))
        self.assertEqual(summary_payload.get("previous_summary"), "")
        unsummarized = summary_payload.get("unsummarized_messages", [])
        self.assertEqual(len(unsummarized), 1)
        self.assertEqual(str(unsummarized[0].get("content", "")), "task start")

        retried_request = llm.calls[2]["messages"]
        assert isinstance(retried_request, list)
        self.assertEqual(str(retried_request[0].get("role", "")), "system")
        self.assertEqual(str(retried_request[1].get("content", "")), "task start")
        self.assertIn("compressed as follows (v1)", str(retried_request[2].get("content", "")))

        self.assertEqual(int(agent.metadata.get("compression_count", 0)), 1)
        self.assertEqual(int(agent.metadata.get("summarized_until_message_index", -1)), 0)

        event_types = [str(entry.get("event_type", "")) for entry in events]
        self.assertIn("context_compacted", event_types)
        self.assertIn("context_overflow_retry", event_types)

    async def test_compress_context_skips_next_context_pressure_reminder_once(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.context.max_context_tokens = 1000
        config.runtime.context.reminder_ratio = 0.6
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient([ChatResult(content="compressed summary body", raw_events=[])])

        result = await runtime.compress_context(
            agent,
            llm_client=llm,
            reason="manual",
        )
        self.assertTrue(bool(result.get("compressed")))
        agent.metadata["current_context_tokens"] = 900

        runtime._maybe_append_context_pressure_reminder(
            agent=agent,
            selected_model=config.llm.openrouter.model_for_role(agent.role.value),
        )
        self.assertFalse(
            any("Context usage warning" in str(message.get("content", "")) for message in agent.conversation)
        )

        runtime._maybe_append_context_pressure_reminder(
            agent=agent,
            selected_model=config.llm.openrouter.model_for_role(agent.role.value),
        )
        self.assertTrue(
            any("Context usage warning" in str(message.get("content", "")) for message in agent.conversation)
        )

    async def test_context_limit_exceeded_forces_preflight_compress(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.context.max_context_tokens = 2000
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        long_chunk = "X" * 8000
        agent.conversation = [
            {"role": "user", "content": "head pinned"},
            {"role": "assistant", "content": long_chunk},
        ]
        agent.metadata["message_index_to_step"] = [0, 0]
        # Preflight compression uses previous real usage, not char-length estimation.
        agent.metadata["current_context_tokens"] = 2600
        llm = RecordingLLMClient(
            [
                ChatResult(content="compressed summary body", raw_events=[]),
                ChatResult(
                    content=json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "done",
                                }
                            ]
                        }
                    ),
                    raw_events=[],
                    usage={"prompt_tokens": 1400},
                ),
            ]
        )

        actions = await runtime.ask(agent, llm_client=llm)

        self.assertEqual(actions[0].get("type"), "finish")
        self.assertEqual(actions[0].get("status"), "completed")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(str(llm.calls[0].get("model", "")), "compress/model")
        self.assertEqual(
            str(llm.calls[1].get("model", "")),
            config.llm.openrouter.model_for_role(agent.role.value),
        )
        request_after = llm.calls[1]["messages"]
        assert isinstance(request_after, list)
        serialized_request_after = json.dumps(request_after, ensure_ascii=False)
        self.assertIn("compressed as follows (v1)", serialized_request_after)
        self.assertNotIn("X" * 256, serialized_request_after)
        self.assertEqual(int(agent.metadata.get("compression_count", 0)), 1)
        event_types = [str(entry.get("event_type", "")) for entry in events]
        self.assertIn("context_limit_forced_compress", event_types)
        self.assertIn("context_compacted", event_types)
        self.assertNotIn(
            "context_pressure_reminder",
            [
                str(entry.get("payload", {}).get("kind", ""))
                for entry in events
                if str(entry.get("event_type", "")) == "control_message"
            ],
        )

    def test_usage_helpers_extract_output_cache_and_total_tokens(self) -> None:
        usage = {
            "input_tokens": 1200,
            "output_tokens": 320,
            "prompt_tokens_details": {
                "cached_tokens": 800,
                "cache_creation_tokens": 64,
            },
        }
        self.assertEqual(usage_output_tokens(usage), 320)
        self.assertEqual(usage_cache_read_tokens(usage), 800)
        self.assertEqual(usage_cache_write_tokens(usage), 64)
        self.assertEqual(usage_total_tokens(usage), 1520)

    def test_usage_helpers_fallback_to_total_minus_input_for_output(self) -> None:
        usage = {
            "total_tokens": 4096,
            "prompt_tokens": 3072,
        }
        self.assertEqual(usage_output_tokens(usage), 1024)

    async def test_overflow_stream_error_event_triggers_forced_compress(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.context.overflow_retry_attempts = 1
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient(
            [
                ChatResult(
                    content="",
                    raw_events=[
                        {
                            "type": "error",
                            "code": "context_window_exceeded",
                            "message": "maximum context length reached",
                        }
                    ],
                ),
                ChatResult(content="compressed summary body", raw_events=[]),
                ChatResult(
                    content=json.dumps(
                        {
                            "actions": [
                                {
                                    "type": "finish",
                                    "status": "completed",
                                    "summary": "done",
                                }
                            ]
                        }
                    ),
                    raw_events=[],
                ),
            ]
        )

        actions = await runtime.ask(agent, llm_client=llm)

        self.assertEqual(actions[0].get("type"), "finish")
        self.assertEqual(actions[0].get("status"), "completed")
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(str(llm.calls[1].get("model", "")), "compress/model")
        event_types = [str(entry.get("event_type", "")) for entry in events]
        self.assertIn("context_compacted", event_types)
        self.assertIn("context_overflow_retry", event_types)

    async def test_overflow_retry_attempt_cap_limits_forced_compression(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.context.enabled = True
        config.runtime.context.compression_model = "compress/model"
        config.runtime.context.overflow_retry_attempts = 1
        events: list[dict[str, Any]] = []
        runtime = self._build_runtime(config=config, event_sink=events)
        agent = self._root_agent()
        llm = RecordingLLMClient(
            [
                ChatResult(
                    content="",
                    raw_events=[],
                    response_error={
                        "code": "context_length_exceeded",
                        "message": "context window exceeded",
                    },
                ),
                ChatResult(content="compressed summary body", raw_events=[]),
                ChatResult(
                    content="",
                    raw_events=[],
                    response_error={
                        "code": "context_window_exceeded",
                        "message": "maximum context reached",
                    },
                ),
            ]
        )

        actions = await runtime.ask(agent, llm_client=llm)

        self.assertEqual(actions[0].get("type"), "finish")
        self.assertEqual(actions[0].get("status"), "partial")
        self.assertEqual(len(llm.calls), 3)
        compression_calls = [call for call in llm.calls if str(call.get("model", "")) == "compress/model"]
        self.assertEqual(len(compression_calls), 1)
        self.assertEqual(int(agent.metadata.get("compression_count", 0)), 1)
