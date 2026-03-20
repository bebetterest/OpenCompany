# 测试与调试模块

## 范围

本模块汇总当前重构后运行时的验证与排障实践。

## 测试面概览

`tests/` 主要分组：

- CLI/配置：`test_cli.py`、`test_config.py`
- prompts/protocol/LLM：`test_prompts.py`、`test_openrouter.py`
- 编排核心：`test_orchestrator_loop.py`、`test_orchestrator_finish.py`、`test_orchestrator_resume.py`、`test_orchestrator_tool_runs.py`
- 工具运行时与执行器：`test_tool_runtime.py`、`test_tools.py`
- 消息接口：`test_messages.py`、`test_message_cursor.py`、`test_message_interface_consistency.py`
- 工作区/沙箱：`test_workspace.py`、`test_sandbox.py`
- UI：`test_tui.py`、`test_webui.py`、`test_webui_api.py`
- 远程 direct 模式：`test_remote.py`（以及 `test_cli.py`、`test_orchestrator.py`、`test_sandbox.py`、`test_webui_api.py` 中的远程分支）

## 推荐验证命令

使用以下任一项目环境方案：

```bash
# 方案 A：Conda
conda run -n OpenCompany pytest

# 方案 B：uv（已创建 .venv 并安装 dev 依赖后）
uv run pytest
```

运行时/工具改动建议补充：

```bash
# 方案 A：Conda
conda run -n OpenCompany pytest tests/test_orchestrator_tool_runs.py tests/test_tool_runtime.py
conda run -n OpenCompany pytest tests/test_orchestrator_resume.py tests/test_message_cursor.py

# 方案 B：uv
uv run pytest tests/test_orchestrator_tool_runs.py tests/test_tool_runtime.py
uv run pytest tests/test_orchestrator_resume.py tests/test_message_cursor.py
```

## CI 自动化

GitHub Actions 通过 `.github/workflows/tests.yml` 执行测试：

- 触发条件：所有 `push`、`pull_request`、`workflow_dispatch`
- 环境：由 `environment.yml` 创建 Conda 环境 `OpenCompany`
- 执行命令：`pytest -q`（全量测试）

## 运行时调试面

- 会话事件流：`.opencompany/sessions/<session_id>/events.jsonl`
- agent 消息流：`.opencompany/sessions/<session_id>/<agent_id>_messages.jsonl`
- 可选 LLM 请求/响应追踪（`--debug`）：`debug/<agent_id>__<module>.jsonl`
- 可选分阶段耗时追踪（`--debug`）：`debug/timings.jsonl`
- 跨层诊断：`.opencompany/diagnostics.jsonl`

常用排障命令：

```bash
opencompany messages <session_id> --include-extra --format text
opencompany tool-runs <session_id> --status running --limit 200
opencompany tool-run-metrics <session_id>
```

`messages --include-extra` 仅包含非工具遥测；工具调用/状态请使用 `tool-runs` 观测。

## 排障检查清单

1. 先用 CLI 输出确认 session 与 pending tool-run 的持久化状态。
2. 对话显示异常时，先查 messages（主源），再查 events（辅助流）。
3. resume 问题优先检查最新 checkpoint 与 pending tool-run 重建逻辑。
4. apply/undo 异常优先检查 session 目录下 project sync 状态与备份清单。
