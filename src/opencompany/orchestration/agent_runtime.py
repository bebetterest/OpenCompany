from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable

from opencompany.config import OpenCompanyConfig
from opencompany.llm.openrouter import ChatResult, OpenRouterClient
from opencompany.models import AgentNode, AgentRole, AgentStatus
from opencompany.orchestration.context import (
    ContextAssembler,
    ContextStore,
    _compression_excluded_message_indices,
    _context_metadata,
    _internal_message_indices,
)
from opencompany.orchestration.messages import (
    assistant_message,
    invalid_response_message,
    tool_result_message,
)
from opencompany.prompts import PromptLibrary
from opencompany.protocol import ProtocolError, extract_json_object, normalize_actions, normalize_tool_calls
from opencompany.utils import stable_json_dumps, utc_now


AgentEventLogger = Callable[..., None]
AgentPersistFn = Callable[[AgentNode], None]
AgentMessageAppendFn = Callable[
    [AgentNode, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None],
    None,
]
AppendSummaryRecordFn = Callable[[AgentNode, dict[str, Any]], None]


class AgentRuntime:
    def __init__(
        self,
        *,
        config: OpenCompanyConfig,
        locale: str,
        prompt_library: PromptLibrary,
        persist_agent: AgentPersistFn,
        log_agent_event: AgentEventLogger,
        append_agent_message: AgentMessageAppendFn,
        append_summary_record: AppendSummaryRecordFn | None = None,
        context_assembler: ContextAssembler | None = None,
        context_store: ContextStore | None = None,
    ) -> None:
        self.config = config
        self.persist_agent = persist_agent
        self.prompt_library = prompt_library
        self.locale = locale
        self.context_assembler = context_assembler or ContextAssembler(
            config=config,
            locale=locale,
            prompt_library=prompt_library,
        )
        self.context_store = context_store or ContextStore(
            locale=locale,
            prompt_library=prompt_library,
            persist_agent=persist_agent,
            append_agent_message=append_agent_message,
        )
        self._log_agent_event = log_agent_event
        self._append_summary_record = append_summary_record or (lambda _agent, _record: None)

    async def ask(
        self,
        agent: AgentNode,
        llm_client: OpenRouterClient | Any | None,
        model_override: str | None = None,
    ) -> list[dict[str, Any]]:
        selected_model = (
            str(model_override or "").strip()
            or self.config.llm.openrouter.model_for_role(agent.role.value)
        )
        agent.status = AgentStatus.RUNNING
        agent.step_count += 1
        if not isinstance(agent.metadata, dict):
            agent.metadata = {}
        agent.metadata["model"] = selected_model
        self.persist_agent(agent)
        if not llm_client:
            raise RuntimeError("OPENROUTER_API_KEY is required to run agents.")
        async def on_token(token: str) -> None:
            self._log_agent_event(
                agent,
                event_type="llm_token",
                phase="llm",
                payload={"token": token},
            )

        async def on_reasoning(token: str) -> None:
            self._log_agent_event(
                agent,
                event_type="llm_reasoning",
                phase="llm",
                payload={"token": token},
            )

        max_empty_response_retries = max(
            0,
            int(self.config.llm.openrouter.empty_response_retries),
        )
        max_overflow_retry_attempts = max(
            0,
            int(self.config.runtime.context.overflow_retry_attempts),
        )
        protocol_retry_attempt = 0
        overflow_retry_attempt = 0
        while True:
            metadata = _context_metadata(agent)
            context_limit_tokens = self._resolved_context_limit_tokens(selected_model)
            context_tokens = self._last_prompt_tokens(agent)
            usage_ratio = (
                round(context_tokens / context_limit_tokens, 4)
                if context_tokens > 0 and context_limit_tokens > 0
                else 0.0
            )
            metadata["context_limit_tokens"] = context_limit_tokens
            metadata["usage_ratio"] = usage_ratio
            if (
                self.config.runtime.context.enabled
                and context_tokens > context_limit_tokens
            ):
                compression_result = await self.compress_context(
                    agent,
                    llm_client=llm_client,
                    reason="forced",
                    overflow_detail={
                        "trigger": "previous_usage_exceeded_max_context_tokens",
                        "current_context_tokens": context_tokens,
                        "context_limit_tokens": context_limit_tokens,
                    },
                )
                self._log_agent_event(
                    agent,
                    event_type="context_limit_forced_compress",
                    phase="context",
                    payload={
                        "compressed": bool(compression_result.get("compressed")),
                        "reason": "previous_usage_exceeded_max_context_tokens",
                        "current_context_tokens": context_tokens,
                        "context_limit_tokens": context_limit_tokens,
                        "usage_ratio": usage_ratio,
                    },
                )
                if bool(compression_result.get("compressed")):
                    continue
            self._maybe_append_context_pressure_reminder(agent=agent, selected_model=selected_model)
            system_prompt = self.context_assembler.system_prompt(agent)
            tools = self.context_assembler.tools(agent)
            request_messages = self.context_assembler.messages(agent, system_prompt)
            conversation_messages = _request_conversation_messages(request_messages)
            self._log_agent_event(
                agent,
                event_type="agent_prompt",
                phase="llm",
                payload={
                    "system_prompt": system_prompt,
                    "messages": conversation_messages,
                    "request_messages": request_messages,
                    "tools": tools,
                    "model": selected_model,
                    "tool_choice": "auto",
                    "parallel_tool_calls": True,
                    "current_context_tokens": context_tokens,
                    "context_limit_tokens": context_limit_tokens,
                    "usage_ratio": usage_ratio,
                    "keep_pinned_messages": max(
                        0,
                        int(self.config.runtime.context.keep_pinned_messages),
                    ),
                    "context_latest_summary": str(
                        metadata.get("context_summary", "") or ""
                    ),
                    "compression_count": self._compression_count(agent),
                    "summary_version": self._summary_version(agent),
                    "summarized_until_message_index": _safe_int(
                        metadata.get("summarized_until_message_index"),
                        default=-1,
                    ),
                    "last_compacted_message_range": metadata.get(
                        "last_compacted_message_range"
                    ),
                    "last_compacted_step_range": metadata.get(
                        "last_compacted_step_range"
                    ),
                    "last_usage_input_tokens": _usage_snapshot_int(
                        metadata.get("last_usage_input_tokens")
                    ),
                    "last_usage_output_tokens": _usage_snapshot_int(
                        metadata.get("last_usage_output_tokens")
                    ),
                    "last_usage_cache_read_tokens": _usage_snapshot_int(
                        metadata.get("last_usage_cache_read_tokens")
                    ),
                    "last_usage_cache_write_tokens": _usage_snapshot_int(
                        metadata.get("last_usage_cache_write_tokens")
                    ),
                    "last_usage_total_tokens": _usage_snapshot_int(
                        metadata.get("last_usage_total_tokens")
                    ),
                },
            )

            started_at = utc_now()
            started_monotonic = time.perf_counter()
            try:
                result = await llm_client.stream_chat(
                    model=selected_model,
                    messages=request_messages,
                    temperature=self.config.llm.openrouter.temperature,
                    max_tokens=self.config.llm.openrouter.max_tokens,
                    debug_agent_id=agent.id,
                    debug_module="agent_runtime.ask",
                    on_token=on_token,
                    on_reasoning=on_reasoning,
                    tools=tools,
                    tool_choice="auto",
                    parallel_tool_calls=True,
                )
            except Exception as exc:
                if (
                    self.config.runtime.context.enabled
                    and overflow_retry_attempt < max_overflow_retry_attempts
                    and _is_context_overflow_exception(exc)
                ):
                    overflow_retry_attempt += 1
                    compression_result = await self.compress_context(
                        agent,
                        llm_client=llm_client,
                        reason="forced",
                        overflow_detail={"error": str(exc)},
                    )
                    self._log_agent_event(
                        agent,
                        event_type="context_overflow_retry",
                        phase="llm",
                        payload={
                            "attempt": overflow_retry_attempt,
                            "max_attempts": max_overflow_retry_attempts,
                            "compressed": bool(compression_result.get("compressed")),
                            "error": str(exc),
                        },
                    )
                    if bool(compression_result.get("compressed")):
                        continue
                raise
            completed_at = utc_now()
            duration_ms = int((time.perf_counter() - started_monotonic) * 1000)
            self._record_usage_metadata(agent, selected_model, result)
            if (
                self.config.runtime.context.enabled
                and overflow_retry_attempt < max_overflow_retry_attempts
                and _is_context_overflow_response(result)
            ):
                overflow_retry_attempt += 1
                compression_result = await self.compress_context(
                    agent,
                    llm_client=llm_client,
                    reason="forced",
                    overflow_detail={"response_error": result.response_error},
                )
                self._log_agent_event(
                    agent,
                    event_type="context_overflow_retry",
                    phase="llm",
                    payload={
                        "attempt": overflow_retry_attempt,
                        "max_attempts": max_overflow_retry_attempts,
                        "compressed": bool(compression_result.get("compressed")),
                        "response_error": result.response_error,
                    },
                )
                if bool(compression_result.get("compressed")):
                    continue
            conversation_message = assistant_message(
                result.content,
                result.tool_calls,
                reasoning=result.reasoning,
                reasoning_details=result.reasoning_details,
            )
            actions: list[dict[str, Any]] | None = None
            assistant_message_internal = False
            protocol_error: ProtocolError | None = None
            try:
                actions = _actions_from_result(result)
                assistant_message_internal = _actions_are_internal_compress(actions)
            except ProtocolError as exc:
                protocol_error = exc
            stored_message = conversation_message
            self.context_store.append_message(
                agent,
                conversation_message,
                stored_message,
                {
                    "source": "llm",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_ms": duration_ms,
                    "response": result.response_payload(stored_message),
                    "stream": {
                        "event_count": len(result.raw_events),
                        "response_object": result.object,
                    },
                    "internal": assistant_message_internal,
                },
            )
            if assistant_message_internal:
                self._mark_latest_conversation_message_internal(agent)
            try:
                if protocol_error is not None:
                    raise protocol_error
                assert actions is not None
                self._log_agent_event(
                    agent,
                    event_type="agent_response",
                    phase="llm",
                    payload={
                        "content": result.content,
                        "reasoning": result.reasoning,
                        "reasoning_details": result.reasoning_details,
                        "tool_calls": _tool_calls_payload(result),
                        "actions": actions,
                    },
                )
                self.persist_agent(agent)
                return actions
            except ProtocolError as exc:
                self._log_agent_event(
                    agent,
                    event_type="agent_response",
                    phase="llm",
                    payload={
                        "content": result.content,
                        "reasoning": result.reasoning,
                        "reasoning_details": result.reasoning_details,
                        "tool_calls": _tool_calls_payload(result),
                    },
                )
                self._log_agent_event(
                    agent,
                    event_type="protocol_error",
                    phase="llm",
                    payload={"error": str(exc), "content": result.content},
                )
                if _should_retry_empty_protocol_response(
                    result=result,
                    error=exc,
                    attempt=protocol_retry_attempt,
                    max_retries=max_empty_response_retries,
                ):
                    protocol_retry_attempt += 1
                    retry_delay_seconds = max(
                        0.0,
                        float(self.config.llm.openrouter.retry_backoff_seconds),
                    ) * (2 ** (protocol_retry_attempt - 1))
                    self._log_agent_event(
                        agent,
                        event_type="protocol_retry",
                        phase="llm",
                        payload={
                            "reason": "empty_response",
                            "attempt": protocol_retry_attempt,
                            "max_retries": max_empty_response_retries,
                            "retry_delay_seconds": retry_delay_seconds,
                            "response_id": result.response_id,
                            "provider": result.provider,
                            "model": result.model,
                        },
                    )
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    continue
                self.context_store.append_message(
                    agent,
                    invalid_response_message(
                        prompt_library=self.prompt_library,
                        locale=self.locale,
                    ),
                    None,
                    None,
                )
                self._log_agent_event(
                    agent,
                    event_type="control_message",
                    phase="llm",
                    payload={
                        "kind": "invalid_response",
                        "content": self.prompt_library.render_runtime_message(
                            "invalid_response",
                            self.locale,
                        ),
                    },
                )
                self.persist_agent(agent)
                fallback_status = "partial" if agent.role == AgentRole.ROOT else "failed"
                fallback_finish: dict[str, Any] = {
                    "type": "finish",
                    "status": fallback_status,
                    "summary": "The agent produced an invalid protocol response.",
                }
                if agent.role != AgentRole.ROOT:
                    fallback_finish["next_recommendation"] = (
                        "Retry with more explicit instructions."
                    )
                return [
                    fallback_finish
                ]

    async def compress_context(
        self,
        agent: AgentNode,
        *,
        llm_client: OpenRouterClient | Any | None,
        reason: str,
        overflow_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not llm_client:
            return {"compressed": False, "error": "OPENROUTER_API_KEY is required."}
        if not self.config.runtime.context.enabled:
            return {"compressed": False, "error": "context compression is disabled."}
        compression_model = str(self.config.runtime.context.compression_model).strip()
        if not compression_model:
            return {
                "compressed": False,
                "error": "runtime.context.compression_model is required for context compression.",
            }

        metadata = _context_metadata(agent)
        previous_summary = str(metadata.get("context_summary", "")).strip()
        summarized_until = _safe_int(metadata.get("summarized_until_message_index"), default=-1)
        internal_indices = _internal_message_indices(agent)
        compression_excluded_indices = _compression_excluded_message_indices(agent)
        unsummarized_rows: list[tuple[int, dict[str, Any]]] = []
        for message_index, message in enumerate(agent.conversation):
            if message_index <= summarized_until:
                continue
            if message_index in internal_indices:
                continue
            if message_index in compression_excluded_indices:
                continue
            unsummarized_rows.append((message_index, message))
        if not unsummarized_rows:
            return {
                "compressed": False,
                "reason": reason,
                "summary_version": self._summary_version(agent),
                "message_range": None,
                "step_range": None,
                "context_tokens_before": self._last_prompt_tokens(agent),
                "context_tokens_after": self._last_prompt_tokens(agent),
                "context_limit_tokens": self._resolved_context_limit_tokens(),
            }

        summarized_message_indices = [int(row[0]) for row in unsummarized_rows]
        message_range = {
            "start": int(summarized_message_indices[0]),
            "end": int(summarized_message_indices[-1]),
        }
        step_range = self._step_range_for_message_indices(agent, summarized_message_indices)
        summary_input_messages = [
            {
                "role": "system",
                "content": self.prompt_library.render_runtime_message(
                    "context_compression_system_prompt",
                    self.locale,
                ),
            },
            {
                "role": "user",
                "content": stable_json_payload(
                    {
                        "previous_summary": previous_summary,
                        "unsummarized_messages": [
                            {
                                "message_index": idx,
                                "role": str(message.get("role", "")),
                                "content": str(message.get("content", "")),
                                "tool_call_id": str(message.get("tool_call_id", "")),
                            }
                            for idx, message in unsummarized_rows
                        ],
                    }
                ),
            },
        ]
        result = await llm_client.stream_chat(
            model=compression_model,
            messages=summary_input_messages,
            temperature=0.0,
            max_tokens=self.config.llm.openrouter.max_tokens,
            timeout_seconds=self._compression_timeout_seconds(),
            debug_agent_id=agent.id,
            debug_module="agent_runtime.compress_context",
            tools=None,
            tool_choice=None,
            parallel_tool_calls=None,
        )
        latest_summary = str(result.content or "").strip()
        if not latest_summary:
            return {"compressed": False, "error": "compression model returned an empty summary."}

        previous_version = self._summary_version(agent)
        summary_version = previous_version + 1
        metadata["context_summary"] = latest_summary
        metadata["summary_version"] = summary_version
        summarized_until_message_index = max(
            int(message_range["end"]),
            len(agent.conversation) - 1,
        )
        metadata["summarized_until_message_index"] = summarized_until_message_index
        metadata["compression_count"] = self._compression_count(agent) + 1
        metadata["last_compacted_message_range"] = message_range
        metadata["last_compacted_step_range"] = step_range
        metadata["skip_next_context_pressure_reminder"] = True
        context_limit_tokens = self._resolved_context_limit_tokens()
        context_tokens_before = self._last_prompt_tokens(agent)
        next_request_messages = self.context_assembler.messages(
            agent,
            self.context_assembler.system_prompt(agent),
        )
        context_tokens_after = self._estimate_prompt_tokens(next_request_messages)
        metadata["context_limit_tokens"] = context_limit_tokens
        metadata["current_context_tokens"] = context_tokens_after
        metadata["usage_ratio"] = (
            round(context_tokens_after / context_limit_tokens, 4)
            if context_tokens_after > 0 and context_limit_tokens > 0
            else 0.0
        )
        self.persist_agent(agent)

        payload = {
            "reason": reason,
            "summary_version": summary_version,
            "summarized_until_message_index": summarized_until_message_index,
            "keep_pinned_messages": max(
                0,
                int(self.config.runtime.context.keep_pinned_messages),
            ),
            "context_latest_summary": latest_summary,
            "message_range": message_range,
            "step_range": step_range,
            "context_tokens_before": context_tokens_before,
            "context_tokens_after": context_tokens_after,
            "context_limit_tokens": context_limit_tokens,
            "compression_count": metadata["compression_count"],
            "overflow_detail": overflow_detail or None,
        }
        self._log_agent_event(
            agent,
            event_type="context_compacted",
            phase="context",
            payload=payload,
        )
        self._append_summary_record(
            agent,
            {
                "timestamp": utc_now(),
                "reason": reason,
                "summary_version": summary_version,
                "message_range": message_range,
                "step_range": step_range,
                "context_tokens_before": context_tokens_before,
                "context_tokens_after": context_tokens_after,
                "context_limit_tokens": context_limit_tokens,
                "summary": latest_summary,
            },
        )
        return {
            "compressed": True,
            "reason": reason,
            "summary_version": summary_version,
            "message_range": message_range,
            "step_range": step_range,
            "context_tokens_before": context_tokens_before,
            "context_tokens_after": context_tokens_after,
            "context_limit_tokens": context_limit_tokens,
        }

    def _compression_count(self, agent: AgentNode) -> int:
        return _safe_int(_context_metadata(agent).get("compression_count"), default=0)

    def _summary_version(self, agent: AgentNode) -> int:
        return max(0, _safe_int(_context_metadata(agent).get("summary_version"), default=0))

    def _last_prompt_tokens(self, agent: AgentNode) -> int:
        return max(0, _safe_int(_context_metadata(agent).get("current_context_tokens"), default=0))

    def _resolved_context_limit_tokens(self, _selected_model: str | None = None) -> int:
        configured_max = int(self.config.runtime.context.max_context_tokens or 0)
        if configured_max <= 0:
            raise ValueError("[runtime.context].max_context_tokens must be > 0.")
        return configured_max

    @staticmethod
    def _estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
        if not messages:
            return 0
        # Cheap cross-model estimate: serialized chars / 4 ~= tokens.
        serialized = stable_json_payload(messages)
        return max(1, int(len(serialized) / 4))

    def _compression_timeout_seconds(self) -> float:
        timeout = self.config.runtime.tool_timeouts.seconds_for(
            "compress_context",
            shell_fallback_seconds=float(self.config.sandbox.timeout_seconds),
        )
        return max(1.0, float(timeout))

    def _record_usage_metadata(
        self,
        agent: AgentNode,
        selected_model: str,
        result: ChatResult,
    ) -> None:
        input_tokens = usage_prompt_tokens(result.usage)
        output_tokens = usage_output_tokens(result.usage)
        cache_read_tokens = usage_cache_read_tokens(result.usage)
        cache_write_tokens = usage_cache_write_tokens(result.usage)
        total_tokens = usage_total_tokens(result.usage)
        context_limit_tokens = self._resolved_context_limit_tokens(selected_model)
        metadata = _context_metadata(agent)
        metadata["context_limit_tokens"] = context_limit_tokens
        if input_tokens is not None:
            metadata["current_context_tokens"] = input_tokens
            metadata["usage_ratio"] = (
                round(input_tokens / context_limit_tokens, 4)
                if context_limit_tokens > 0
                else 0.0
            )
        _set_optional_non_negative_int(
            metadata,
            key="last_usage_input_tokens",
            value=input_tokens,
        )
        _set_optional_non_negative_int(
            metadata,
            key="last_usage_output_tokens",
            value=output_tokens,
        )
        _set_optional_non_negative_int(
            metadata,
            key="last_usage_cache_read_tokens",
            value=cache_read_tokens,
        )
        _set_optional_non_negative_int(
            metadata,
            key="last_usage_cache_write_tokens",
            value=cache_write_tokens,
        )
        _set_optional_non_negative_int(
            metadata,
            key="last_usage_total_tokens",
            value=total_tokens,
        )
        metadata["last_context_usage_recorded_at"] = utc_now()
        self.persist_agent(agent)

    def _maybe_append_context_pressure_reminder(
        self,
        *,
        agent: AgentNode,
        selected_model: str,
    ) -> None:
        if not self.config.runtime.context.enabled:
            return
        metadata = _context_metadata(agent)
        if bool(metadata.pop("skip_next_context_pressure_reminder", False)):
            self.persist_agent(agent)
            return
        context_tokens = _safe_int(metadata.get("current_context_tokens"), default=0)
        context_limit = self._resolved_context_limit_tokens(selected_model)
        if context_tokens <= 0 or context_limit <= 0:
            return
        ratio = context_tokens / context_limit
        if ratio < float(self.config.runtime.context.reminder_ratio):
            return
        reminder_content = self.prompt_library.render_runtime_message(
            "context_pressure_reminder",
            self.locale,
            current_context_tokens=context_tokens,
            context_limit_tokens=context_limit,
            usage_ratio=f"{ratio:.4f}",
        )
        reminder_message = {"role": "user", "content": reminder_content}
        self.context_store.append_message(
            agent,
            reminder_message,
            None,
            {"exclude_from_context_compression": True},
        )
        self._log_agent_event(
            agent,
            event_type="control_message",
            phase="context",
            payload={
                "kind": "context_pressure_reminder",
                "content": reminder_content,
                "current_context_tokens": context_tokens,
                "context_limit_tokens": context_limit,
                "usage_ratio": round(ratio, 4),
            },
        )

    @staticmethod
    def _mark_latest_conversation_message_internal(agent: AgentNode) -> None:
        if not agent.conversation:
            return
        metadata = _context_metadata(agent)
        raw_indices = metadata.get("internal_message_indices")
        normalized: list[int]
        if isinstance(raw_indices, list):
            normalized = []
            for value in raw_indices:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if index >= 0:
                    normalized.append(index)
        else:
            normalized = []
        latest_index = len(agent.conversation) - 1
        if latest_index not in normalized:
            normalized.append(latest_index)
            metadata["internal_message_indices"] = sorted(set(normalized))
            # Keep metadata compact while preserving ordering semantics.
            if len(metadata["internal_message_indices"]) > 2000:
                metadata["internal_message_indices"] = metadata["internal_message_indices"][-2000:]
            # Agent metadata mutates in-memory; caller persists via existing runtime path.

    @staticmethod
    def _step_range_for_message_indices(
        agent: AgentNode,
        message_indices: list[int],
    ) -> dict[str, int] | None:
        normalized_indices = sorted({index for index in message_indices if index >= 0})
        if not normalized_indices:
            return None
        metadata = _context_metadata(agent)
        raw_step_map = metadata.get("message_index_to_step")
        step_values: list[int] = []
        if isinstance(raw_step_map, list):
            for message_index in normalized_indices:
                if message_index < 0 or message_index >= len(raw_step_map):
                    continue
                step_value = _safe_int(raw_step_map[message_index], default=0)
                if step_value > 0:
                    step_values.append(step_value)
        if not step_values:
            step = max(1, int(agent.step_count))
            return {"start": step, "end": step}
        return {
            "start": min(step_values),
            "end": max(step_values),
        }

    def append_tool_result(
        self,
        agent: AgentNode,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        internal = str(action.get("type", "")).strip() == "compress_context"
        self.context_store.append_message(
            agent,
            tool_result_message(
                action,
                result,
                prompt_library=self.prompt_library,
                locale=self.locale,
            ),
            None,
            {"internal": internal},
        )
        if internal:
            self._mark_latest_conversation_message_internal(agent)
        self.persist_agent(agent)


def _request_conversation_messages(request_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if request_messages and str(request_messages[0].get("role", "")) == "system":
        return request_messages[1:]
    return request_messages


def _actions_from_result(result: ChatResult) -> list[dict[str, Any]]:
    if result.tool_calls:
        actions = normalize_tool_calls(result.tool_calls)
        if actions:
            return actions
    payload = extract_json_object(result.content)
    return normalize_actions(payload)


def _should_retry_empty_protocol_response(
    *,
    result: ChatResult,
    error: ProtocolError,
    attempt: int,
    max_retries: int,
) -> bool:
    if attempt >= max_retries:
        return False
    if str(error).strip() != "No JSON object found in model response":
        return False
    if result.tool_calls:
        return False
    if result.content.strip():
        return False
    if result.response_error is not None:
        return False
    return True


def _tool_calls_payload(result: ChatResult) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for tool_call in result.tool_calls:
        try:
            arguments = extract_json_object(tool_call.arguments_json)
        except ProtocolError:
            arguments = {"raw_arguments": tool_call.arguments_json}
        payload.append(
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": arguments,
            }
        )
    return payload


def _actions_are_internal_compress(actions: list[dict[str, Any]]) -> bool:
    if not actions:
        return False
    for action in actions:
        if str(action.get("type", "")).strip() != "compress_context":
            return False
    return True


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _non_negative_int(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return numeric


def _usage_snapshot_int(value: Any) -> int | None:
    return _non_negative_int(value)


def _set_optional_non_negative_int(
    metadata: dict[str, Any],
    *,
    key: str,
    value: int | None,
) -> None:
    if value is None:
        metadata.pop(key, None)
        return
    metadata[key] = max(0, int(value))


def _usage_value(usage: dict[str, Any] | None, *keys: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        if not key:
            continue
        value = _non_negative_int(usage.get(key))
        if value is not None:
            return value
    return None


def _usage_nested_value(
    usage: dict[str, Any] | None,
    *,
    detail_keys: tuple[str, ...],
    value_keys: tuple[str, ...],
) -> int | None:
    if not isinstance(usage, dict):
        return None
    for detail_key in detail_keys:
        details = usage.get(detail_key)
        if not isinstance(details, dict):
            continue
        value = _usage_value(details, *value_keys)
        if value is not None:
            return value
    return None


def usage_prompt_tokens(usage: dict[str, Any] | None) -> int | None:
    return _usage_value(usage, "prompt_tokens", "input_tokens")


def usage_output_tokens(usage: dict[str, Any] | None) -> int | None:
    direct = _usage_value(
        usage,
        "output_tokens",
        "completion_tokens",
        "assistant_tokens",
        "generated_tokens",
        "response_tokens",
    )
    if direct is not None:
        return direct
    total_tokens = _usage_value(usage, "total_tokens")
    baseline = usage_prompt_tokens(usage)
    if total_tokens is not None and baseline is not None:
        return max(0, total_tokens - baseline)
    return None


def usage_total_tokens(usage: dict[str, Any] | None) -> int | None:
    direct = _usage_value(usage, "total_tokens", "tokens")
    if direct is not None:
        return direct
    input_tokens = usage_prompt_tokens(usage)
    output_tokens = usage_output_tokens(usage)
    if input_tokens is None and output_tokens is None:
        return None
    return max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))


def usage_cache_read_tokens(usage: dict[str, Any] | None) -> int | None:
    direct = _usage_value(
        usage,
        "cache_read_tokens",
        "cached_tokens",
        "cache_hit_tokens",
        "prompt_cache_hit_tokens",
        "input_cached_tokens",
        "input_cache_read_tokens",
        "cache_read_input_tokens",
    )
    if direct is not None:
        return direct
    return _usage_nested_value(
        usage,
        detail_keys=("prompt_tokens_details", "input_tokens_details", "cache_details"),
        value_keys=(
            "cached_tokens",
            "cache_read_tokens",
            "cache_hit_tokens",
            "prompt_cache_hit_tokens",
            "input_cached_tokens",
            "read_tokens",
        ),
    )


def usage_cache_write_tokens(usage: dict[str, Any] | None) -> int | None:
    direct = _usage_value(
        usage,
        "cache_write_tokens",
        "cache_creation_tokens",
        "prompt_cache_write_tokens",
        "input_cache_write_tokens",
        "cache_creation_input_tokens",
    )
    if direct is not None:
        return direct
    return _usage_nested_value(
        usage,
        detail_keys=("prompt_tokens_details", "input_tokens_details", "cache_details"),
        value_keys=(
            "cache_write_tokens",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "write_tokens",
            "created_tokens",
        ),
    )


def stable_json_payload(value: Any) -> str:
    return stable_json_dumps(value)


_CONTEXT_OVERFLOW_KEYWORDS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "token limit",
    "context size",
)
_CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "prompt_too_long",
    "token_limit_exceeded",
}


def _is_context_overflow_exception(exc: Exception) -> bool:
    raw_text = str(exc or "")
    if _contains_context_overflow_text(raw_text):
        return True
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in {400, 413}:
        response = getattr(exc, "response", None)
        payload = None
        if response is not None:
            try:
                payload = response.json()
            except Exception:
                payload = None
        if _contains_context_overflow_payload(payload):
            return True
    return False


def _is_context_overflow_response(result: ChatResult) -> bool:
    if _contains_context_overflow_payload(result.response_error):
        return True
    for event in result.raw_events:
        if isinstance(event, dict):
            if _contains_context_overflow_payload(event.get("error")):
                return True
            if _contains_context_overflow_payload(event):
                return True
    return False


def _contains_context_overflow_payload(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, str):
        return _contains_context_overflow_text(payload)
    if isinstance(payload, list):
        return any(_contains_context_overflow_payload(item) for item in payload)
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "type", "code"):
            if key in payload and _contains_context_overflow_payload(payload.get(key)):
                return True
        code = str(payload.get("code", "")).strip().lower()
        if code in _CONTEXT_OVERFLOW_CODES:
            return True
        nested_error = payload.get("error")
        if nested_error is not None and _contains_context_overflow_payload(nested_error):
            return True
    return False


def _contains_context_overflow_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _CONTEXT_OVERFLOW_KEYWORDS):
        return True
    if any(code in normalized for code in _CONTEXT_OVERFLOW_CODES):
        return True
    return False
