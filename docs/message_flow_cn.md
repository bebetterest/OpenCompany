# Message Flow

本文说明当前运行时协议：统一 `finish`、持久化 `tool_run` 生命周期，以及 root agent/worker agent 共用同一循环模型。

## 消息通道

OpenCompany 并行维护四条通道：

1. `agent.conversation`
   单个 agent 的可重放历史；这是后续轮次唯一会回传给模型的历史。
2. API 工具字段
   `tools`、`tool_choice`、`parallel_tool_calls` 通过 API 字段发送，不混入消息正文。
3. 运行时事件
   结构化事件（`agent_prompt`、`agent_response`、`tool_call`、`tool_run_*`、`agent_completed` 等）用于 UI 与调试。
4. 每 agent 消息日志
   `sessions/<session_id>/<agent_id>_messages.jsonl` 存储重建消息和响应元数据。

## message-first 实时重建

TUI / Web UI 的 `Agents` 实时视图应以每个 agent 的 message 日志作为主重建来源，而不是直接由 runtime events 重建主对话内容。

- 主内容来源：`*_messages.jsonl`（`user` / `assistant` / `tool` messages）。
- runtime events 职责：状态/活动/工作流更新与运行遥测诊断。
- 工具调用步骤与 tool-run 状态迁移由 runtime events 跟踪，但展示层统一走 Tool Runs 视图（CLI `tool-runs`、UI `Tool Runs` 页签），不再进入 agent 对话面板。
- agent 实时面板只展示 message 派生条目（包含 role=`tool` 的工具返回内容）。
- runtime extra 流条目（`*_preview`、`*_extra`）不在 agent 实时面板展示。
- 可见 message 条目渲染策略：仅 LLM 类型（`thinking`、`reply`、`response`）按 Markdown；其余 message 类型按转义纯文本展示；若内容可解析为 JSON，则格式化后展示。

## 统一请求组装

每次 `AgentRuntime.ask()` 都按以下结构组装：

```python
messages = [{"role": "system", "content": system_prompt}, *prompt_window]
```

其中：

- `system_prompt` 按角色与 locale 选择。
- `prompt_window` 始终排除内部压缩轨迹消息。
- 无 summary 时：`prompt_window = 全部非 internal 消息`。
- 有 summary 时：`prompt_window = 头部 pinned 消息 + 合成 summary + summarized_until_message_index 之后的非 internal 消息`。
- `tools` 按角色从配置化工具注册表选择。
- root agent 与 worker agent 统一使用收尾工具 `finish`。
- `agent_prompt` 事件从同一请求快照给出两种视图：
  - `request_messages`：发送给 LLM API 的完整消息数组
  - `messages`：仅 conversation 视图（即去掉首条 `system` 后的 `request_messages`）

实时消息分页也遵循同一 prompt window：

- `/api/session/{id}/messages` 与 `orchestrator.list_session_messages(...)` 会为每条 message 注入 `prompt_visible` 与 `prompt_bucket`。
- `prompt_bucket` 取值为 `pinned`、`tail`、`hidden_middle`、`internal`。
- UI 实时面板只渲染 `prompt_visible=true` 的消息，再在最后一条 `pinned` 消息与第一条 `tail` 消息之间插入合成 summary 行。

## Tool run 一等公民状态

每个工具动作都会写入持久化 `tool_run`：

- `id`（`tool_run_id`）
- `session_id`、`agent_id`
- `tool_name`、`arguments`
- 生命周期时间戳（`created_at`、`started_at`、`completed_at`）
- `status`（`queued`、`running`、`completed`、`failed`、`cancelled`）
- 原始 `result` / `error`

同一次工具执行在运行时维护两种视图：

- 存储/调试视图：完整原始 `tool_run.result`
- agent 可见视图：投影后的精简 payload，会写入对话

这种分层能在不污染模型上下文的前提下保留完整可观测性。

## agent 可见工具返回协议

agent 可见工具返回统一为：

- 不再注入拼接型 `summary` 字符串
- 仅保留下轮决策需要的最小结构化字段
- 正常成功响应不回显低价值入参

列表型工具（`list_agent_runs`、`list_tool_runs`）统一分页形态：

- 请求：`limit`、`cursor`
- 返回：`next_cursor`、`has_more`
- 默认 `limit` 由 `[runtime.tools].list_default_limit` 控制（默认 20）

tool-run 查询行为：

- `list_tool_runs`：仅返回概览行
- `get_tool_run`：默认概览（对 `shell` 运行中会带 `stdout`/`stderr` 快照）；仅 `include_result=true` 时带原始 `result`
- `wait_run`：状态型返回（`wait_run_status`，必要时附超时/错误标记）
- `cancel_tool_run`：最小返回（`final_status`、`cancelled_agents_count`）

## 工具调用语义

- 大多数工具调用统一按阻塞模式处理。`shell` 在超过前台等待阈值时可能提前返回 `status=running`、`background=true` 与 `tool_run_id`，命令会继续在后台运行。
- `spawn_agent` 创建 child 后会在同一轮立即返回 `child_agent_id` 与 `tool_run_id`。
- tool schema 不再提供按调用覆盖的阻塞参数。

## spawn 语义

`spawn_agent` 行为：

1. runtime 创建 child 与 child workspace。
2. 同一轮返回 `tool_run_id` 与 `child_agent_id`。
3. spawn 对应 tool run 在创建步骤内即完成。
4. child 在自己的 loop 中执行。
5. 若该分支已不再需要，可调用 `cancel_agent(agent_id)` 取消。
6. child 完成总结不会自动注入父级上下文；父 agent/root agent 需显式检查子 agent 状态。

## finish 语义

root agent 与 worker agent 都调用 `finish`，由 runtime 按角色校验：

- root：`status` 枚举包含 `interrupted`；不允许 `next_recommendation`
- worker：`status` 枚举不包含 `interrupted`；允许 `next_recommendation`

`finish` schema 不包含阻塞字段。
runtime 若检测到仍有未完成 child，会拒绝 `finish`。
runtime 会在执行前拒绝非法角色字段组合。

## 步数上限与强制总结

- worker 软阈值：worker 的 `step_count` 达到 `max_agent_steps` 后，runtime 会按间隔注入收尾提醒，并允许继续后续轮次。
- root 软阈值：root 的 `step_count` 达到 `max_root_steps` 后，runtime 会按间隔注入收尾提醒，并允许继续后续轮次。

## Checkpoint 与恢复

checkpoint 包含：

- session 状态
- agent 状态
- workspace 状态
- pending agent IDs
- pending tool run IDs
- root agent loop 索引

恢复时，runtime 会恢复 checkpoint 中的状态（session/agents/workspaces/pending IDs），并在需要时重建 spawn run 与 child 关联。

## 推荐协作模式

```json
{
  "actions": [
    {"type": "spawn_agent", "instruction": "检查模块 A"},
    {"type": "list_agent_runs", "limit": 20}
  ]
}
```

随后：

```json
{
  "actions": [
    {"type": "wait_time", "seconds": 10},
    {"type": "list_agent_runs", "limit": 20},
    {"type": "finish", "status": "completed", "summary": "已整合子结果。"}
  ]
}
```
