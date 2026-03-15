from __future__ import annotations

from pathlib import Path
from typing import Any

from opencompany.config import RuntimeLimitsConfig
from opencompany.models import AgentRole, ToolCall
from opencompany.prompts import PromptLibrary, default_prompts_dir
from opencompany.utils import stable_json_dumps


def _prompt_library(prompt_library: PromptLibrary | None) -> PromptLibrary:
    return prompt_library or PromptLibrary(default_prompts_dir())


def root_initial_message(
    task: str,
    project_dir: Path,
    limits: RuntimeLimitsConfig,
    *,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> str:
    return _prompt_library(prompt_library).render_runtime_message(
        "root_initial",
        locale,
        task=task,
        project_dir=project_dir,
        max_children_per_agent=limits.max_children_per_agent,
        max_active_agents=limits.max_active_agents,
        max_root_steps=limits.max_root_steps,
    )


def worker_initial_message(
    instruction: str,
    workspace_path: Path,
    *,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> str:
    return _prompt_library(prompt_library).render_runtime_message(
        "worker_initial",
        locale,
        instruction=instruction,
        workspace_path=workspace_path,
    )


def invalid_response_message(
    *,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> dict[str, str]:
    return {
        "role": "user",
        "content": _prompt_library(prompt_library).render_runtime_message(
            "invalid_response",
            locale,
        ),
    }


def step_limit_summary_message(
    *,
    max_steps: int,
    reason: str,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> dict[str, str]:
    return {
        "role": "user",
        "content": _prompt_library(prompt_library).render_runtime_message(
            "step_limit_worker",
            locale,
            reason=reason,
            max_steps=max_steps,
        ),
    }


def unfinished_children_message(
    *,
    role: AgentRole,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> dict[str, str]:
    message_key = (
        "unfinished_children_root" if role == AgentRole.ROOT else "unfinished_children_worker"
    )
    return {
        "role": "user",
        "content": _prompt_library(prompt_library).render_runtime_message(
            message_key,
            locale,
        ),
    }


def assistant_message(
    content: str,
    tool_calls: list[ToolCall],
    *,
    reasoning: str = "",
    reasoning_details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if reasoning:
        message["reasoning"] = reasoning
    if reasoning_details:
        message["reasoning_details"] = reasoning_details
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments_json,
                },
            }
            for tool_call in tool_calls
        ]
    return message


def tool_result_message(
    action: dict[str, Any],
    tool_result: dict[str, Any],
    *,
    prompt_library: PromptLibrary | None = None,
    locale: str = "en",
) -> dict[str, Any]:
    serialized_result = _message_tool_result(action, tool_result)
    tool_call_id = action.get("_tool_call_id")
    if tool_call_id:
        return {
            "role": "tool",
            "tool_call_id": str(tool_call_id),
            "content": stable_json_dumps(serialized_result),
        }
    return {
        "role": "user",
        "content": _prompt_library(prompt_library).render_runtime_message(
            "tool_result_fallback",
            locale,
            action_json=stable_json_dumps(_public_action(action)),
            result_json=stable_json_dumps(serialized_result),
        ),
    }


def _public_action(action: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in action.items() if key != "_tool_call_id"}


def _message_tool_result(
    action: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    if action.get("type") != "shell" or "command" not in tool_result:
        return tool_result
    sanitized = dict(tool_result)
    sanitized.pop("command", None)
    return sanitized
