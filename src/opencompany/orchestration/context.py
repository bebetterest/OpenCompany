from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from opencompany.config import OpenCompanyConfig
from opencompany.models import AgentNode
from opencompany.orchestration.messages import tool_result_message
from opencompany.prompts import PromptLibrary
from opencompany.skills import render_skills_prompt
from opencompany.tools import tool_definitions_for_role


PersistAgentFn = Callable[[AgentNode], None]
AppendAgentMessageFn = Callable[
    [AgentNode, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None],
    None,
]


@dataclass(frozen=True, slots=True)
class PromptWindowProjection:
    summary: str
    summary_version: int
    pinned_message_indices: tuple[int, ...]
    tail_message_indices: tuple[int, ...]
    hidden_message_indices: tuple[int, ...]
    internal_message_indices: tuple[int, ...]

    @property
    def prompt_message_indices(self) -> tuple[int, ...]:
        return (*self.pinned_message_indices, *self.tail_message_indices)

    def bucket_for_message_index(self, message_index: int) -> str:
        if message_index in self.internal_message_indices:
            return "internal"
        if message_index in self.pinned_message_indices:
            return "pinned"
        if message_index in self.tail_message_indices:
            return "tail"
        return "hidden_middle"


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
        prompt = self.prompt_library.load(agent.role.value, self.locale)
        metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
        skills_catalog = metadata.get("skills_catalog")
        if not isinstance(skills_catalog, dict):
            return prompt
        skills_prompt = render_skills_prompt(
            locale=self.locale,
            bundle_root=str(skills_catalog.get("bundle_root", "") or ""),
            manifest_path=str(skills_catalog.get("manifest_path", "") or ""),
            skills_state=skills_catalog,
        )
        if not skills_prompt:
            return prompt
        return f"{prompt.rstrip()}\n\n{skills_prompt}"

    def tools(self, agent: AgentNode) -> list[dict[str, object]]:
        return tool_definitions_for_role(
            agent.role,
            self.locale,
            config=self.config,
            prompt_library=self.prompt_library,
        )

    def messages(self, agent: AgentNode, system_prompt: str) -> list[dict[str, Any]]:
        keep_count = max(0, int(self.config.runtime.context.keep_pinned_messages))
        projection = prompt_window_projection(
            agent,
            keep_pinned_messages=keep_count,
        )
        prompt_messages = [
            agent.conversation[index]
            for index in projection.prompt_message_indices
            if 0 <= index < len(agent.conversation)
        ]
        if not projection.summary:
            return [{"role": "system", "content": system_prompt}, *prompt_messages]

        pinned_count = len(projection.pinned_message_indices)
        pinned_head = prompt_messages[:pinned_count]
        tail_messages = prompt_messages[pinned_count:]
        latest_summary_message = {
            "role": "user",
            "content": self.prompt_library.render_runtime_message(
                "context_latest_summary",
                self.locale,
                summary_version=max(1, projection.summary_version),
                summary=projection.summary,
            ),
        }
        return [
            {"role": "system", "content": system_prompt},
            *pinned_head,
            latest_summary_message,
            *tail_messages,
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


def _metadata_message_indices(metadata: dict[str, Any], key: str) -> set[int]:
    raw_indices = metadata.get(key)
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


def _internal_message_indices(agent: AgentNode) -> set[int]:
    return _metadata_message_indices(_context_metadata(agent), "internal_message_indices")


def _compression_excluded_message_indices(agent: AgentNode) -> set[int]:
    return _metadata_message_indices(
        _context_metadata(agent),
        "compression_excluded_message_indices",
    )


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


def prompt_window_projection_from_metadata(
    *,
    message_count: int,
    metadata: dict[str, Any] | None,
    keep_pinned_messages: int,
) -> PromptWindowProjection:
    normalized_metadata = metadata if isinstance(metadata, dict) else {}
    normalized_message_count = max(0, int(message_count))
    internal_indices = _metadata_message_indices(
        normalized_metadata,
        "internal_message_indices",
    )
    ordered_visible_indices = [
        index for index in range(normalized_message_count) if index not in internal_indices
    ]
    summary = str(normalized_metadata.get("context_summary", "")).strip()
    raw_summary_version = _safe_int(normalized_metadata.get("summary_version"), default=0)
    if not summary:
        return PromptWindowProjection(
            summary="",
            summary_version=max(0, raw_summary_version),
            pinned_message_indices=(),
            tail_message_indices=tuple(ordered_visible_indices),
            hidden_message_indices=(),
            internal_message_indices=tuple(sorted(internal_indices)),
        )

    keep_count = max(0, int(keep_pinned_messages))
    pinned_indices = ordered_visible_indices[:keep_count] if keep_count > 0 else []
    pinned_index_set = set(pinned_indices)
    summarized_until = _safe_int(
        normalized_metadata.get("summarized_until_message_index"),
        default=-1,
    )
    tail_indices = [
        index
        for index in ordered_visible_indices
        if index > summarized_until and index not in pinned_index_set
    ]
    tail_index_set = set(tail_indices)
    hidden_indices = [
        index
        for index in ordered_visible_indices
        if index not in pinned_index_set and index not in tail_index_set
    ]
    return PromptWindowProjection(
        summary=summary,
        summary_version=max(1, raw_summary_version),
        pinned_message_indices=tuple(pinned_indices),
        tail_message_indices=tuple(tail_indices),
        hidden_message_indices=tuple(hidden_indices),
        internal_message_indices=tuple(sorted(internal_indices)),
    )


def prompt_window_projection(
    agent: AgentNode,
    *,
    keep_pinned_messages: int,
) -> PromptWindowProjection:
    return prompt_window_projection_from_metadata(
        message_count=len(agent.conversation),
        metadata=_context_metadata(agent),
        keep_pinned_messages=keep_pinned_messages,
    )


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
