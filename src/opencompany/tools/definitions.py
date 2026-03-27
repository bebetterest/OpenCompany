from __future__ import annotations

import copy

from opencompany.config import OpenCompanyConfig, RuntimeToolsConfig
from opencompany.models import AgentRole
from opencompany.prompts import PromptLibrary, default_prompts_dir

DEFAULT_TOOL_NAMES = (
    "shell",
    "compress_context",
    "wait_time",
    "list_agent_runs",
    "get_agent_run",
    "spawn_agent",
    "cancel_agent",
    "steer_agent",
    "list_tool_runs",
    "get_tool_run",
    "wait_run",
    "cancel_tool_run",
    "finish",
)

ROOT_TOOL_NAMES = DEFAULT_TOOL_NAMES
WORKER_TOOL_NAMES = DEFAULT_TOOL_NAMES
PAGINATED_TOOL_NAMES = frozenset(
    {
        "list_agent_runs",
        "list_tool_runs",
    }
)

BOUNDARY_TOOL_NAMES = frozenset(
    {"spawn_agent", "wait_run", "cancel_tool_run", "cancel_agent", "steer_agent"}
)
TERMINAL_TOOL_NAMES = frozenset({"finish"})
CONTROL_TOOL_NAMES = frozenset(BOUNDARY_TOOL_NAMES | TERMINAL_TOOL_NAMES)


def _normalize_tool_locale(locale: str | None) -> str:
    return "zh" if locale == "zh" else "en"


def _prompt_library(
    prompt_library: PromptLibrary | None,
) -> PromptLibrary:
    if prompt_library is not None:
        return prompt_library
    return PromptLibrary(default_prompts_dir())


def tool_definitions_for_role(
    role: AgentRole | str,
    locale: str | None = None,
    *,
    config: OpenCompanyConfig | None = None,
    prompt_library: PromptLibrary | None = None,
) -> list[dict[str, object]]:
    normalized_role = AgentRole(role)
    normalized_locale = _normalize_tool_locale(locale)
    default_list_limit, max_list_limit = RuntimeToolsConfig().list_limit_bounds()
    wait_time_min_seconds, wait_time_max_seconds = RuntimeToolsConfig().wait_time_bounds()
    if config is not None:
        names = config.runtime.tools.tool_names_for_role(normalized_role.value)
        default_list_limit, max_list_limit = config.runtime.tools.list_limit_bounds()
        wait_time_min_seconds, wait_time_max_seconds = config.runtime.tools.wait_time_bounds()
    else:
        names = list(ROOT_TOOL_NAMES if normalized_role == AgentRole.ROOT else WORKER_TOOL_NAMES)
    blueprints = _prompt_library(prompt_library).load_tool_definitions(normalized_locale)
    resolved: list[dict[str, object]] = []
    for name in names:
        if name not in blueprints:
            continue
        tool = copy.deepcopy(blueprints[name])
        if name in PAGINATED_TOOL_NAMES:
            _apply_list_limit_schema(
                tool,
                name=name,
                locale=normalized_locale,
                default_limit=default_list_limit,
                max_limit=max_list_limit,
            )
        if name == "wait_time":
            _apply_wait_time_schema(
                tool,
                locale=normalized_locale,
                minimum_seconds=wait_time_min_seconds,
                maximum_seconds=wait_time_max_seconds,
            )
        if name == "finish":
            _apply_finish_role_schema(tool, normalized_role)
        resolved.append(tool)
    return resolved


def _apply_finish_role_schema(tool: dict[str, object], role: AgentRole) -> None:
    function = tool.get("function")
    if not isinstance(function, dict):
        return
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return

    status_schema = properties.get("status")
    if isinstance(status_schema, dict):
        enum_values = status_schema.get("enum")
        if isinstance(enum_values, list):
            if role == AgentRole.WORKER:
                status_schema["enum"] = [
                    value for value in enum_values if str(value).strip() != "interrupted"
                ]
            elif role == AgentRole.ROOT:
                status_schema["enum"] = [
                    value
                    for value in enum_values
                    if str(value).strip() in {"completed", "partial"}
                ]

    if role == AgentRole.ROOT:
        properties.pop("next_recommendation", None)


def _apply_list_limit_schema(
    tool: dict[str, object],
    *,
    name: str,
    locale: str,
    default_limit: int,
    max_limit: int,
) -> None:
    function = tool.get("function")
    if not isinstance(function, dict):
        return
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return
    limit_schema = properties.get("limit")
    if not isinstance(limit_schema, dict):
        return
    limit_schema["minimum"] = 1
    limit_schema["maximum"] = max_limit
    limit_schema["default"] = default_limit
    limit_schema["description"] = _list_limit_description(
        tool_name=name,
        locale=locale,
        default_limit=default_limit,
        max_limit=max_limit,
    )


def _apply_wait_time_schema(
    tool: dict[str, object],
    *,
    locale: str,
    minimum_seconds: float,
    maximum_seconds: float,
) -> None:
    function = tool.get("function")
    if not isinstance(function, dict):
        return
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return
    seconds_schema = properties.get("seconds")
    if not isinstance(seconds_schema, dict):
        return
    seconds_schema["minimum"] = _schema_number(minimum_seconds)
    seconds_schema["maximum"] = _schema_number(maximum_seconds)
    seconds_schema["description"] = _wait_time_seconds_description(
        locale=locale,
        minimum_seconds=minimum_seconds,
        maximum_seconds=maximum_seconds,
    )


def _list_limit_description(
    *,
    tool_name: str,
    locale: str,
    default_limit: int,
    max_limit: int,
) -> str:
    noun_en = {
        "list_agent_runs": "agent runs",
        "list_tool_runs": "records",
    }.get(tool_name, "items")
    noun_zh = {
        "list_agent_runs": "agent 运行",
        "list_tool_runs": "记录",
    }.get(tool_name, "条目")
    if locale == "zh":
        return f"每页返回的最大{noun_zh}数。默认 {default_limit}，最大 {max_limit}。"
    return (
        f"Maximum number of {noun_en} to return per page. "
        f"Default {default_limit}; max {max_limit}."
    )


def _wait_time_seconds_description(
    *,
    locale: str,
    minimum_seconds: float,
    maximum_seconds: float,
) -> str:
    minimum_text = f"{minimum_seconds:g}"
    maximum_text = f"{maximum_seconds:g}"
    if locale == "zh":
        return f"必填等待秒数，必须 >= {minimum_text} 且 <= {maximum_text}。"
    return (
        "Required wait duration in seconds. "
        f"Must be >= {minimum_text} and <= {maximum_text}."
    )


def _schema_number(value: float) -> int | float:
    integral = int(value)
    if float(integral) == float(value):
        return integral
    return float(value)
