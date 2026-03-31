# 编排流水线模块

## 范围

本模块覆盖以下实现：

- `opencompany/orchestration/agent_loop.py`
- `opencompany/orchestration/agent_runtime.py`
- `opencompany/orchestration/context.py`
- `opencompany/orchestration/messages.py`
- `opencompany/orchestrator.py` 中的集成逻辑

## 循环引擎（`AgentLoopRunner`）

`AgentLoopRunner.run(...)` 与角色无关，统一执行：

1. 向 agent 请求 actions
2. 执行动作批次
3. 若出现 finish payload 则终止
4. 超出步数后注入软性收尾提醒（不做硬性强制 finish）

返回结构：

- `finish_payload`
- `interrupted`
- `step_limit_reached`

## Runtime 调度

`opencompany/orchestrator.py` 现在把每个活跃 agent 当作独立可运行单元：

- root 和 worker 分别运行在各自的 runtime task 中
- task 唤醒由事件驱动（`spawn_agent`、steer 重新激活、任务完成/取消、pending tool run 重建）
- tool 执行仍然只阻塞调用方本身，但不会再因为兄弟 agent 而出现会话级整批等待屏障
- `pending_agent_ids` 保留为 checkpoint/调试快照，不再是调度器的唯一队列来源

## Agent 运行时（`AgentRuntime`）

`AgentRuntime.ask(...)` 负责：

- 按角色选择 prompt（root/worker）
- 注入工具 schema
- 一次组装请求消息，并复用于事件记录与 LLM API 调用
- 流式 token/reasoning 事件记录
- assistant message 持久化（含响应与耗时元数据）
- 协议解析并归一为 actions

空协议重试：

- 仅在“结构性空响应且仍有重试额度”时触发
- 每次空协议重试会以 `llm_retry` 事件记录（`retry_reason=empty_protocol_response`），并与 API/网络/空流重试共用统一统计口径
- 否则注入 invalid-response 控制消息并走失败型 fallback `finish`

## 上下文组装与写入

`ContextAssembler`：

- 按角色与 locale 选择系统提示词
- 按角色取工具集合（配置 + prompt schema）
- 组装请求消息（`system` + conversation）

MCP 集成后，上下文组装会新增两层运行时内容：

- 追加 agent 级 MCP prompt block，汇总当前启用 servers、连接 warning 与已发现数量
- 将内建工具、MCP helper 工具以及已连接 servers 动态发现出的 MCP tool 定义合并成 agent 当前可见的工具表面

`ContextStore`：

- 追加 conversation / tool-result 消息
- 回调持久化层与消息日志器

## 控制消息路径

运行时会在非理想路径注入控制消息，例如：

- 协议响应无效
- worker 软步骤阈值收尾提醒
- 有未完成子分支时拒绝 finish
- root 软步骤阈值收尾提醒
- spawn 分支取消时的“取消总结请求”提示

## 子总结传递

已完成子分支总结不会自动注入父级上下文：

- 父级需要通过 tool-run 相关工具显式查看进度与结果
- 取消链路仍会尽力在取消结果中带回一次“取消总结”

## 恢复语义

上下文导入 / 继续执行时：

- 根据 checkpoint 重建 session/agent/workspace 图
- conversation 优先由持久化 `*_messages.jsonl` 重建（checkpoint conversation 仅兜底）
- 将活跃 agent 统一规范为 `paused`，并取消其 queued/running tool run，然后立即写入新 checkpoint
- 继续执行必须走 `resume(session_id, instruction)`：先给 root 追加一条 user 消息，再重新进入事件驱动的 runtime loop
