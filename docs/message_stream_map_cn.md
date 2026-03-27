# 消息流梳理

本文按 CLI/TUI/Web UI 逐项梳理“界面里每种内容来自哪里”。

## 规范化数据源

- 每 agent 消息日志：`sessions/<session_id>/<agent_id>_messages.jsonl`
  - 模型可见对话主源（`user` / `assistant` / `tool` 角色）。
  - 每条记录包含 `step_count`（写入时的 agent 运行步号），用于 live 界面把兜底/系统消息归到正确步骤分组。
- 运行时事件日志：`sessions/<session_id>/events.jsonl`
  - 运行遥测与生命周期事件主源。
- Tool run 持久化状态：`tool_runs`（SQLite + API/CLI）
- 工具生命周期（`queued/running/completed/failed/cancelled/abandoned`）主源。

## 各界面映射

| 界面 | 主对话面板来源 | 不在主对话面板展示 | 工具生命周期展示位置 |
|---|---|---|---|
| Web UI `Agents` | `/api/session/{id}/messages` 回放 | 全部 runtime extra 条目（`*_preview`、`*_extra`） | Web UI `Tool Runs` 页签（`/api/session/{id}/tool-runs`） |
| TUI `Agents` | `orchestrator.list_session_messages(...)` 回放 | 全部 runtime extra 条目（`*_preview`、`*_extra`） | TUI `Tool Runs` 页签 |
| CLI `messages --include-extra` | `list_session_messages(...)` 的消息分页 | `llm_token`、`llm_reasoning`、`tool_call_started`、`tool_call`、`tool_run_submitted`、`tool_run_updated` | CLI `tool-runs` / `tool-run-metrics` |

## runtime 事件去向速览

- `llm_token`、`llm_reasoning`
  - 会落盘到 `events.jsonl`。
  - 不在 agents 实时对话面板展示。
- `tool_call_started`、`tool_call`、`tool_run_submitted`、`tool_run_updated`
  - 会落盘到 `events.jsonl`。
  - 用于触发 Tool Runs 数据刷新。
  - 不在 agents 实时对话面板展示。
- `shell_stream`
  - 会落盘到 `events.jsonl`。
  - 在 agents 实时对话面板隐藏（仍可通过日志/CLI extra 查看）。
- `control_message`、`protocol_error`、`sandbox_violation`、`agent_completed`
  - 会落盘到 `events.jsonl`。
  - 在 agents 实时对话面板隐藏（保留用于诊断/activity/CLI extra）。

## 排障建议顺序

1. 对话显示异常：先查 `*_messages.jsonl`（`opencompany messages ...`）。
2. 工具执行异常：查 `tool-runs` / `tool-run-metrics`。
3. 运行时异常（超时/错误/控制消息）：查 `events.jsonl` 或 CLI `--include-extra`。

## Live 步骤分组规则

- 主规则（按消息序列）：
  - `assistant` 消息：开启/推进当前步骤。
  - `tool` / `user` 消息：归到上一条 assistant 所在步骤。
- `step_count`（如果存在）作为下限，避免显示步号回退到 runtime 已达到步号之前。
