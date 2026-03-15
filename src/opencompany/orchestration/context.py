from __future__ import annotations

from typing import Any, Callable

from opencompany.config import OpenCompanyConfig
from opencompany.models import AgentNode
from opencompany.orchestration.messages import tool_result_message
from opencompany.prompts import PromptLibrary
from opencompany.tools import tool_definitions_for_role


PersistAgentFn = Callable[[AgentNode], None]
AppendAgentMessageFn = Callable[
    [AgentNode, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None],
    None,
]


class ContextAssembler:
    """Builds role-aware LLM requests from the agent conversation state."""

    def __init__(
        self,
        *,
        config: OpenCompanyConfig,
        locale: str,
        prompt_library: PromptLibrary,
    ) -> None:
        self.config = config
        self.locale = locale
        self.prompt_library = prompt_library

    def system_prompt(self, agent: AgentNode) -> str:
        return self.prompt_library.load(agent.role.value, self.locale)

    def tools(self, agent: AgentNode) -> list[dict[str, object]]:
        return tool_definitions_for_role(
            agent.role,
            self.locale,
            config=self.config,
            prompt_library=self.prompt_library,
        )

    def messages(self, agent: AgentNode, system_prompt: str) -> list[dict[str, Any]]:
        conversation = _conversation_without_internal_messages(agent)
        summary = str(_context_metadata(agent).get("context_summary", "")).strip()
        if not summary:
            return [{"role": "system", "content": system_prompt}, *conversation]

        keep_count = max(0, int(self.config.runtime.context.keep_pinned_messages))
        pinned_head = conversation[:keep_count] if keep_count > 0 else []
        summary_version = _safe_int(_context_metadata(agent).get("summary_version"), default=1)
        summarized_until = _safe_int(
            _context_metadata(agent).get("summarized_until_message_index"),
            default=-1,
        )
        unsummarized_messages = _unsummarized_messages(
            agent,
            summarized_until_message_index=summarized_until,
        )
        latest_summary_message = {
            "role": "user",
            "content": self.prompt_library.render_runtime_message(
                "context_latest_summary",
                self.locale,
                summary_version=max(1, summary_version),
                summary=summary,
            ),
        }
        return [
            {"role": "system", "content": system_prompt},
            *pinned_head,
            latest_summary_message,
            *unsummarized_messages,
        ]


class ContextStore:
    """Owns conversation writes so runtime/message persistence stays centralized."""

    def __init__(
        self,
        *,
        locale: str,
        prompt_library: PromptLibrary,
        persist_agent: PersistAgentFn,
        append_agent_message: AppendAgentMessageFn,
    ) -> None:
        self.locale = locale
        self.prompt_library = prompt_library
        self.persist_agent = persist_agent
        self.append_agent_message = append_agent_message

    def append_message(
        self,
        agent: AgentNode,
        message: dict[str, Any],
        stored_message: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.append_agent_message(agent, message, stored_message, metadata)

    def append_tool_result(
        self,
        agent: AgentNode,
        action: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.append_message(
            agent,
            tool_result_message(
                action,
                result,
                prompt_library=self.prompt_library,
                locale=self.locale,
            ),
        )
        self.persist_agent(agent)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _context_metadata(agent: AgentNode) -> dict[str, Any]:
    if not isinstance(agent.metadata, dict):
        agent.metadata = {}
    return agent.metadata


def _internal_message_indices(agent: AgentNode) -> set[int]:
    metadata = _context_metadata(agent)
    raw_indices = metadata.get("internal_message_indices")
    if not isinstance(raw_indices, list):
        return set()
    normalized: set[int] = set()
    for value in raw_indices:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index >= 0:
            normalized.add(index)
    return normalized


def _compression_excluded_message_indices(agent: AgentNode) -> set[int]:
    metadata = _context_metadata(agent)
    raw_indices = metadata.get("compression_excluded_message_indices")
    if not isinstance(raw_indices, list):
        return set()
    normalized: set[int] = set()
    for value in raw_indices:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index >= 0:
            normalized.add(index)
    return normalized


def _conversation_without_internal_messages(agent: AgentNode) -> list[dict[str, Any]]:
    internal_indices = _internal_message_indices(agent)
    if not internal_indices:
        return list(agent.conversation)
    filtered: list[dict[str, Any]] = []
    for index, message in enumerate(agent.conversation):
        if index in internal_indices:
            continue
        filtered.append(message)
    return filtered


def _unsummarized_messages(
    agent: AgentNode,
    *,
    summarized_until_message_index: int,
) -> list[dict[str, Any]]:
    internal_indices = _internal_message_indices(agent)
    unsummarized: list[dict[str, Any]] = []
    for index, message in enumerate(agent.conversation):
        if index <= summarized_until_message_index:
            continue
        if index in internal_indices:
            continue
        unsummarized.append(message)
    return unsummarized
