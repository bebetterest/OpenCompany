from __future__ import annotations

from typing import Any

from opencompany.config import OpenCompanyConfig
from opencompany.models import AgentNode, AgentRole
from opencompany.prompts import PromptLibrary
from opencompany.tools.definitions import tool_definitions_for_role

MCP_HELPER_TOOL_NAMES = frozenset(
    {
        "list_mcp_servers",
        "list_mcp_resources",
        "read_mcp_resource",
    }
)


def _agent_mcp_state(agent: AgentNode) -> dict[str, Any]:
    metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
    state = metadata.get("mcp")
    return state if isinstance(state, dict) else {}


def _mcp_enabled(agent: AgentNode) -> bool:
    state = _agent_mcp_state(agent)
    return bool(state.get("enabled", False))


def agent_dynamic_tool_definitions(agent: AgentNode) -> list[dict[str, Any]]:
    state = _agent_mcp_state(agent)
    entries = state.get("dynamic_tools")
    if not isinstance(entries, list):
        return []
    return [
        dict(item)
        for item in entries
        if isinstance(item, dict)
    ]


def visible_tool_names_for_agent(
    agent: AgentNode,
    *,
    config: OpenCompanyConfig,
) -> tuple[str, ...]:
    names = list(config.runtime.tools.tool_names_for_role(agent.role.value))
    if not _mcp_enabled(agent):
        names = [name for name in names if name not in MCP_HELPER_TOOL_NAMES]
    dynamic_names = [
        str(item.get("function", {}).get("name", "")).strip()
        for item in agent_dynamic_tool_definitions(agent)
        if isinstance(item.get("function"), dict)
    ]
    return tuple(
        name
        for name in [*names, *dynamic_names]
        if name
    )


def tool_definitions_for_agent(
    agent: AgentNode,
    *,
    locale: str | None,
    config: OpenCompanyConfig,
    prompt_library: PromptLibrary | None = None,
) -> list[dict[str, Any]]:
    builtins = tool_definitions_for_role(
        AgentRole(agent.role),
        locale,
        config=config,
        prompt_library=prompt_library,
    )
    if not _mcp_enabled(agent):
        builtins = [
            tool
            for tool in builtins
            if str(tool.get("function", {}).get("name", "")).strip()
            not in MCP_HELPER_TOOL_NAMES
        ]
    return [*builtins, *agent_dynamic_tool_definitions(agent)]
