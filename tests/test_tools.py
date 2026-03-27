from __future__ import annotations

import unittest

from opencompany.config import OpenCompanyConfig
from opencompany.models import AgentRole
from opencompany.tools import tool_definitions_for_role


def _tool_by_name(tools: list[dict[str, object]], name: str) -> dict[str, object]:
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return tool
    raise AssertionError(f"Tool {name} not found")


def _strip_descriptions(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_descriptions(item)
            for key, item in value.items()
            if key != "description"
        }
    if isinstance(value, list):
        return [_strip_descriptions(item) for item in value]
    return value


class ToolDefinitionTests(unittest.TestCase):
    def test_tool_definitions_default_to_english(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT)

        list_agent_runs = _tool_by_name(tools, "list_agent_runs")
        function = list_agent_runs["function"]
        assert isinstance(function, dict)
        self.assertEqual(
            function["description"],
            "List agent runs in this session, newest-to-oldest by created_at.",
        )
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        status_schema = properties["status"]
        assert isinstance(status_schema, dict)
        self.assertEqual(
            status_schema["description"],
            "Optional status filter. Accepts one string or an array of strings; values are matched as-is after normalization.",
        )

    def test_tool_definitions_localize_to_chinese(self) -> None:
        tools = tool_definitions_for_role(AgentRole.WORKER, "zh")

        shell = _tool_by_name(tools, "shell")
        shell_function = shell["function"]
        assert isinstance(shell_function, dict)
        self.assertEqual(
            shell_function["description"],
            "在分配到的工作区内运行受限 shell 命令。",
        )
        shell_parameters = shell_function["parameters"]
        assert isinstance(shell_parameters, dict)
        shell_properties = shell_parameters["properties"]
        assert isinstance(shell_properties, dict)
        cwd_schema = shell_properties["cwd"]
        assert isinstance(cwd_schema, dict)
        self.assertEqual(cwd_schema["description"], "可选的工作区相对工作目录，默认 '.'.")

        finish = _tool_by_name(tools, "finish")
        finish_function = finish["function"]
        assert isinstance(finish_function, dict)
        finish_parameters = finish_function["parameters"]
        assert isinstance(finish_parameters, dict)
        finish_properties = finish_parameters["properties"]
        assert isinstance(finish_properties, dict)
        summary_schema = finish_properties["summary"]
        assert isinstance(summary_schema, dict)
        self.assertEqual(
            summary_schema["description"],
            "必填：对已完成工作、剩余缺口和关键结果的简明总结（无默认值）。",
        )

        next_schema = finish_properties["next_recommendation"]
        assert isinstance(next_schema, dict)
        self.assertEqual(
            next_schema["description"],
            "仅 worker 使用的建议字段。worker 在 status 为 'partial' 或 'failed' 时必须提供；root 会忽略该字段。",
        )

        compress_tool = _tool_by_name(tools, "compress_context")
        compress_function = compress_tool["function"]
        assert isinstance(compress_function, dict)
        self.assertEqual(
            compress_function["description"],
            "当上下文较多且任务进入新阶段、适合对之前上下文减负时使用。压缩会抹除具体轨迹细节，因此应尽量在可分割节点调用。调用 compress_context 时不要在同一轮同时调用其他工具，以免造成干扰。",
        )

    def test_compress_context_tool_definition_warns_against_mixing_other_tools(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        compress_tool = _tool_by_name(tools, "compress_context")
        function = compress_tool["function"]
        assert isinstance(function, dict)
        self.assertEqual(
            function["description"],
            "Use this when context is long and work has entered a new phase where earlier history can be compacted. Compression removes fine-grained trajectory details, so call it at a clean split point whenever possible. Do not call other tools in the same response as compress_context; use it alone to avoid interference.",
        )

    def test_tool_locales_share_the_same_schema_shape(self) -> None:
        english_tools = tool_definitions_for_role(AgentRole.WORKER, "en")
        chinese_tools = tool_definitions_for_role(AgentRole.WORKER, "zh")

        self.assertEqual(
            _strip_descriptions(english_tools),
            _strip_descriptions(chinese_tools),
        )

    def test_spawn_agent_schema_does_not_include_child_summary_injection_toggle(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        spawn_tool = _tool_by_name(tools, "spawn_agent")
        function = spawn_tool["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertNotIn("inject_child_summary", properties)

    def test_get_agent_run_schema_does_not_include_inject_child_summary(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        get_agent_run = _tool_by_name(tools, "get_agent_run")
        function = get_agent_run["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertNotIn("inject_child_summary", properties)

    def test_get_agent_run_schema_allows_negative_message_indexes(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        get_agent_run = _tool_by_name(tools, "get_agent_run")
        function = get_agent_run["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        start_schema = properties["messages_start"]
        end_schema = properties["messages_end"]
        assert isinstance(start_schema, dict)
        assert isinstance(end_schema, dict)
        self.assertNotIn("minimum", start_schema)
        self.assertNotIn("minimum", end_schema)

    def test_list_style_tools_include_pagination_fields(self) -> None:
        tools = tool_definitions_for_role(AgentRole.WORKER, "en")
        for name in ("list_agent_runs", "list_tool_runs"):
            tool = _tool_by_name(tools, name)
            function = tool["function"]
            assert isinstance(function, dict)
            parameters = function["parameters"]
            assert isinstance(parameters, dict)
            properties = parameters["properties"]
            assert isinstance(properties, dict)
            self.assertIn("limit", properties)
            self.assertIn("cursor", properties)

    def test_steer_agent_schema_requires_agent_id_and_content(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        steer_agent = _tool_by_name(tools, "steer_agent")
        function = steer_agent["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(parameters.get("required"), ["agent_id", "content"])
        self.assertIn("agent_id", properties)
        self.assertIn("content", properties)

    def test_list_style_tool_limits_follow_runtime_configuration(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.tools.list_default_limit = 37
        config.runtime.tools.list_max_limit = 81
        tools = tool_definitions_for_role(AgentRole.ROOT, "en", config=config)
        for name in ("list_agent_runs", "list_tool_runs"):
            tool = _tool_by_name(tools, name)
            function = tool["function"]
            assert isinstance(function, dict)
            parameters = function["parameters"]
            assert isinstance(parameters, dict)
            properties = parameters["properties"]
            assert isinstance(properties, dict)
            limit_schema = properties["limit"]
            assert isinstance(limit_schema, dict)
            self.assertEqual(limit_schema.get("minimum"), 1)
            self.assertEqual(limit_schema.get("maximum"), 81)
            self.assertEqual(limit_schema.get("default"), 37)

    def test_wait_time_tool_schema_is_blocking_only_and_requires_seconds(self) -> None:
        tools = tool_definitions_for_role(AgentRole.WORKER, "en")
        wait_tool = _tool_by_name(tools, "wait_time")
        function = wait_tool["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertIn("seconds", properties)
        self.assertNotIn("blocking", properties)
        seconds_schema = properties["seconds"]
        assert isinstance(seconds_schema, dict)
        self.assertEqual(seconds_schema.get("type"), "number")
        self.assertEqual(seconds_schema.get("minimum"), 10)
        self.assertEqual(seconds_schema.get("maximum"), 60)
        required = parameters.get("required")
        self.assertEqual(required, ["seconds"])

    def test_wait_time_tool_schema_follows_runtime_configuration(self) -> None:
        config = OpenCompanyConfig()
        config.runtime.tools.wait_time_min_seconds = 4
        config.runtime.tools.wait_time_max_seconds = 9
        tools = tool_definitions_for_role(AgentRole.WORKER, "en", config=config)
        wait_tool = _tool_by_name(tools, "wait_time")
        function = wait_tool["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        seconds_schema = properties["seconds"]
        assert isinstance(seconds_schema, dict)
        self.assertEqual(seconds_schema.get("minimum"), 4)
        self.assertEqual(seconds_schema.get("maximum"), 9)
        self.assertIn(">= 4", str(seconds_schema.get("description")))
        self.assertIn("<= 9", str(seconds_schema.get("description")))

    def test_compress_context_tool_schema_has_no_parameters(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        compress_tool = _tool_by_name(tools, "compress_context")
        function = compress_tool["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties, {})
        self.assertNotIn("required", parameters)

    def test_list_tool_runs_schema_includes_cursor(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        list_tool_runs = _tool_by_name(tools, "list_tool_runs")
        function = list_tool_runs["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertIn("cursor", properties)
        cursor_schema = properties["cursor"]
        assert isinstance(cursor_schema, dict)
        self.assertEqual(cursor_schema.get("type"), "string")

    def test_get_tool_run_schema_includes_include_result(self) -> None:
        tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        get_tool_run = _tool_by_name(tools, "get_tool_run")
        function = get_tool_run["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        self.assertIn("include_result", properties)
        include_result_schema = properties["include_result"]
        assert isinstance(include_result_schema, dict)
        self.assertEqual(include_result_schema.get("type"), "boolean")

    def test_finish_schema_is_role_specific_and_has_no_blocking(self) -> None:
        root_tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        worker_tools = tool_definitions_for_role(AgentRole.WORKER, "en")
        root_finish = _tool_by_name(root_tools, "finish")
        worker_finish = _tool_by_name(worker_tools, "finish")

        root_fn = root_finish["function"]
        worker_fn = worker_finish["function"]
        assert isinstance(root_fn, dict)
        assert isinstance(worker_fn, dict)
        root_params = root_fn["parameters"]
        worker_params = worker_fn["parameters"]
        assert isinstance(root_params, dict)
        assert isinstance(worker_params, dict)
        root_props = root_params["properties"]
        worker_props = worker_params["properties"]
        assert isinstance(root_props, dict)
        assert isinstance(worker_props, dict)
        self.assertNotIn("blocking", root_props)
        self.assertNotIn("blocking", worker_props)
        self.assertNotIn("next_recommendation", root_props)
        self.assertIn("next_recommendation", worker_props)

        root_status = root_props["status"]
        worker_status = worker_props["status"]
        assert isinstance(root_status, dict)
        assert isinstance(worker_status, dict)
        root_enum = root_status.get("enum")
        worker_enum = worker_status.get("enum")
        self.assertIsInstance(root_enum, list)
        self.assertIsInstance(worker_enum, list)
        self.assertEqual(set(root_enum), {"completed", "partial"})
        self.assertNotIn("interrupted", worker_enum)
        self.assertIn("failed", worker_enum)

    def test_cancel_tool_run_schema_is_available(self) -> None:
        english_tools = tool_definitions_for_role(AgentRole.ROOT, "en")
        chinese_tools = tool_definitions_for_role(AgentRole.WORKER, "zh")
        cancel_en = _tool_by_name(english_tools, "cancel_tool_run")
        cancel_zh = _tool_by_name(chinese_tools, "cancel_tool_run")
        for cancel_tool in (cancel_en, cancel_zh):
            function = cancel_tool["function"]
            assert isinstance(function, dict)
            parameters = function["parameters"]
            assert isinstance(parameters, dict)
            required = parameters.get("required")
            self.assertEqual(required, ["tool_run_id"])
