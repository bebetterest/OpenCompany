# 运行时核心模块

## 范围

本模块说明 `opencompany/orchestrator.py` 中的会话级运行时行为：

- 会话启动（`run_task`）、元数据加载（`load_session_context`）、显式克隆（`clone_session`）与继续执行（`resume`）
- root/worker 生命周期协同
- 全局限制、中断、失败处理
- root 收尾与项目暂存同步入口

## 生命周期模型

会话状态主流程：

1. `running`：由 `run_task` 或 `resume(session_id, instruction)` 进入
2. `completed`：由 `_finalize_root` 设置
3. `interrupted`：由 `_mark_interrupted` 设置
4. `failed`：由 `_mark_failed` 设置

Agent 状态：

- `pending`、`running`、`paused`、`completed`、`failed`、`cancelled`、`terminated`
- worker 还会记录 `completion_status`（`completed`/`partial`/`failed`，取消链路可见 `cancelled`）
- 会话 `completion_state` 仅表示完成质量（`completed`/`partial`），且只有 `session.status=completed` 时才允许非空。

## 调度模型

- 活跃 root 和 worker 都会作为独立的 asyncio task 运行在同一个会话级 runtime loop 中。
- tool 调用通常只会阻塞发起调用的 agent；但 `shell` 在超过前台等待阈值后可提前返回 `status=running` 并后台继续执行。除非显式 `wait_run` 等待，兄弟 agent 不会互相卡住。
- runtime 的唤醒改为事件驱动：`spawn_agent`、steer 重新激活、任务完成/取消、pending tool run 重建都会直接触发调度，而不是依赖 `pending_agent_ids` 的整批 drain。
- checkpoint 仍会记录 `pending_agent_ids`，但该字段现在只是“当前活跃 worker”的派生快照，用于调试和恢复提示，不再是调度器的唯一输入。

## 运行预算与限制

限制来自 `opencompany.toml` 的 `[runtime.limits]`：

- `max_children_per_agent`
- `max_active_agents`
- `max_root_steps`
- `max_agent_steps`

行为影响：

- root 软步骤阈值（`max_root_steps`）：按间隔注入收尾提醒，会话继续运行。
- worker 软步骤阈值（`max_agent_steps`）：按间隔注入收尾提醒，不走强制 fallback finish。
- 子 agent 扇出上限：超限时阻止新的 `spawn_agent`。
- 活跃 agent 上限：通过 worker 调度并发控制生效。

## 上下文压缩运行时

运行时上下文压缩由 `[runtime.context]` 控制：

- `reminder_ratio`（默认 `0.8`）：当最近一次 prompt 使用率超过该阈值时，每轮注入压缩提醒。
- `keep_pinned_messages`（默认 `1`）：进入 summary 模式后仍保留的头部消息数量；这里按 message 计数，不按 step 计数。
- `max_context_tokens`（必填，且 `> 0`）：上下文窗口长度的唯一来源。
- `compression_model`（压缩时必填）：仅用于压缩的固定模型。
- `overflow_retry_attempts`（默认 `1`）：超窗后“强制压缩 + 重试”的最大次数。
- 请求前强制预检：每次调用 LLM 前，运行时会基于上一轮真实输入 usage（`current_context_tokens`）判断；若其超过 `max_context_tokens`，即使 provider 尚未报超窗错误，也会先强制执行 `compress_context`。
- 无论手动压缩还是强制压缩，只要压缩成功，运行时都会把这条“请求前强制预检压缩”判断在下一整个 agent step 内都跳过；如果下一步里发生了内部重试，该步内的所有重试也都会继续跳过这条预检。
- 压缩调用超时：使用 `runtime.tool_timeouts.actions.compress_context`（默认 `180s`）。

上下文上限来源：

1. `max_context_tokens`

压缩算法为替换式：

- 输入：`previous_summary + unsummarized_messages`
- 输出：`latest_summary`（覆盖旧 summary，不做 summary 拼接）
- 软阈值提醒消息（`root_loop_force_finalize`、worker 步数提醒）与上下文压力提醒消息不会进入压缩输入
- 当前 step 的压缩请求消息仍会进入压缩输入；但它在普通 prompt/UI 组装里依旧保持 internal
- 强制压缩不会把当前正在进行的 step 纳入压缩输入/压缩范围，因为 runtime 在压缩后还会继续这个 step，下一步也仍需要看到这个进行中的 step 信息
- 如果同一步同时发出了 `compress_context` 和其他工具，runtime 会先等其他工具完成，再执行压缩，这样压缩输入里拿到的是这些工具的最终返回结果
- 如果同一步错误地同时发出了 `finish` 和其他工具，runtime 也会把 `finish` 延后到普通工具和 `compress_context` 之后，避免压缩因 action 顺序被直接跳过
- 强制压缩成功后，runtime 仍会补写 internal 的请求/结果 marker；但 `summarized_until_message_index` 只会停在“实际被总结的最后一条非当前 step 消息”上，这样当前进行中的 step 还能留给后续一步继续使用
- metadata 持久化：`context_summary`、`summary_version`、`summarized_until_message_index`、`compression_count`、`last_compacted_message_range`、`last_compacted_step_range`
- 派生日志：`<agent_id>_summaries.jsonl`

请求组装规则：

- 首次 summary 之前：`system + conversation`
- 有 summary 后：`system + pinned_head + latest_summary + unsummarized_messages`
- 已被 summary 覆盖的历史消息不会再进入请求
- 压缩内部控制痕迹（internal）不会进入请求

## Root 与 Worker 边界

root 协调者：

- 负责重评上下文、分派子任务、跟踪 tool runs。
- 存在活跃子分支（`pending`/`running`）时不得 `finish`。
- `finish` 映射为会话级字段（`completion_state`、`user_summary`）；`follow_up_needed` 仍为编排层内部推导/维护的元数据。

worker：

- 在隔离工作区执行任务。
- 通过 `finish` 返回（`status`、`summary`、`next_recommendation`）。
- 完成后先把文件增量提升到父工作区，再进入 root 收尾。
- 每个新 root/worker run 的首条用户消息都会带一个身份前缀块，说明该 agent 自身的 name/id，以及父agent 的 name/id（若不存在父agent 也会显式说明）。

## Steer Run 投递

- 运行时支持按 agent 维度的 steer run，生命周期为：`waiting -> completed | cancelled`。
- UI/用户 steer 与 agent 工具 `steer_agent` 会进入同一条 `submit_steer_run(...)` 提交流程。
- 每条 steer run 同时记录目标 agent（`agent_id`）和来源 actor 快照（`source_agent_id`、`source_agent_name`）。
- 每次 agent 调用 LLM 之前，会按创建顺序加载该 agent 的 `waiting` steers。
- 当会话仍是 `running` 且 steer 提交目标是不可调度 agent（`paused`/`completed`/`failed`/`cancelled`/`terminated`）时，运行时会将该 agent 重新激活为 `running`，并立刻重新进入正常会话调度。
- 运行时会在持久化/投递的 steer 文本顶部插入本地化引导提示，并追加本地化来源署名（英文为 `--- from ...`，中文为 `--- 来自于 ...`）。
- 当 steer 目标 agent 当前处于 `completed` 时，运行时会在署名之后再追加一句提醒，要求执行新指令后再调用一次 `finish` 工具收尾。
- 每条 steer 都是一次性消费：
  - 先执行 CAS 状态迁移 `waiting -> completed`。
  - 再将 steer 内容按“每条一条”追加到对话历史（`role=user`）。
  - message 日志 metadata 记录 `source=steer`、`steer_run_id`、`steer_source`、`steer_source_agent_id`、`steer_source_agent_name`、`delivered_step`。
- 取消只允许对 `waiting` 生效（CAS 迁移 `waiting -> cancelled`），`completed`/`cancelled` 为终态。

## 导入与继续语义

- `load_session_context(session_id)` 现在只是只读元数据加载：优先返回持久化 session 行（checkpoint 中的 session 负载仅作兜底），不会 clone、不会导入 conversation，也不会修改运行时状态。
- `clone_session(session_id)` 会显式深拷贝 session 目录、checkpoints、messages、events、tool runs、steer runs 与 agent 行；clone 血缘通过 `continued_from_session_id` 与 `continued_from_checkpoint_seq` 记录。
- `_import_session_context(session_id, source)` 才负责从 checkpoint 恢复会话、agent 图和 workspaces，并优先通过 `*_messages.jsonl` 重建 conversation（checkpoint conversation 仅兜底）。
- 导入时会把活跃 agent（`pending`/`running`）统一规范为 `paused`；关联的 queued/running tool run 在 `source=run` 下会标记为 `cancelled`，在 `source=resume` 下会标记为 `abandoned`，然后立即写入新 checkpoint。
- 导入/恢复时，可运行 agent 会根据实时 agent 状态与 pending tool runs 重新推导；持久化的 `pending_agent_ids` 只作为派生元数据参考。
- 中断路径会将活跃 agent（`pending`/`running`）标记为 `terminated`，取消 pending tool runs，会话标记为 `interrupted`，并持久化 checkpoint。
- `continued_from_session_id` 现在只会来自显式 `clone_session(...)`；在 UI/TUI/CLI 中单纯加载 session 不会再产生新的 lineage 节点。
- `resume(session_id, instruction)` 现在必须提供非空 instruction；默认 `run_root_agent=True` 时，运行前会给 root 追加一条新的 `user` 消息，再切到 `running` 继续循环。
- `resume(...)` 可选接收 `reactivate_agent_id`；当目标 agent 属于不可调度状态（`paused`/`completed`/`failed`/`cancelled`/`terminated`）时，runtime 会在调度前将其重新激活为 `running`。
- `run_task_in_session(session_id, task)` 会先导入上下文，再为本次运行追加一个全新 root agent（新 ID），更新 `session.root_agent_id` 并执行该新 root；历史 root 会保留，便于区分不同轮次执行轨迹。
- `resume(..., run_root_agent=True)` 场景下若 `reactivate_agent_id` 指向某个 root agent，runtime 会在调度前把 `session.root_agent_id` 切到该目标 root，从而只执行被 steer 的 root 分支。
- `resume(...)` 可选接收 `run_root_agent=False`（且 `reactivate_agent_id` 必须是非 root agent），此时会执行聚焦 worker 相关的活跃 worker 分支，且该轮不会重新激活 root；只有全部 agent 进入终态才会收敛到会话 `completed`，否则保持 `running`。
- UI 在活跃会话中可按 agent 子树执行终止（`terminate_agent_subtree(session_id, agent_id)`）：目标及其全部后代会被标记为 `cancelled`（`completion_status=cancelled`），并同步取消对应活跃 root/worker 任务与关联的 queued/running tool runs。
- session_id 在访问会话路径前会按安全 slug 规则校验（`[A-Za-z0-9][A-Za-z0-9_-]*`）。
- `paused` 是不可调度状态，调度器不会自动恢复。

## 远程会话运行时（Direct 模式）

- 远程会话元数据按会话持久化在 `.opencompany/sessions/<session_id>/remote_session.json`。
- 仅当会话模式为 `direct` 时允许远程工作目录；`staged + remote` 会被拒绝。
- 远程配置在会话上下文导入时读取一次并缓存到当前会话运行时。
- `remote_session.json` 不会保存明文密码。
- 对于 password auth，首次成功输入后可复用本地安全凭据存储；当 OS 凭据后端不可用时，会使用本地加密回退存储。
- 会话完成/中断/失败时会触发远程运行时清理（SSH 控制状态 + 临时缓存残留）。

## MCP 会话状态

- 会话现在会和 skills 一起持久化 `enabled_mcp_server_ids` 与 `mcp_state`。
- MCP server 定义来自 `opencompany.toml` 中的 `[mcp]` / `[mcp.servers.<id>]`；单次 run/resume 选择的是会话级启用集合，可不同于配置默认值。
- 每个 agent 都使用自己独立的 MCP 连接，因此不同 workspace 下的 root/worker 不会共享 roots 或 tool/resource 缓存。
- runtime 会在 agent 第一次 LLM step 前准备 MCP 连接，在后续 step 中保持复用，并在 agent/session 结束时关闭。
- resume/import 会保留启用的 server ids；缺失或损坏的 servers 会作为 `session.mcp_state` 中的 warning 暴露，而不是静默忽略。

## 收尾与用户确认

当 root 以 `completed` 或 `partial` 收尾时：

1. 运行时先暂存 root 工作区增量。
2. 会话总结追加明确 apply/undo 提示。
3. 只有用户执行 `opencompany apply <session_id>`（或 UI apply）后才写回目标项目。

撤销路径：

- `opencompany undo <session_id>` 使用备份元数据回滚。

## 集成注意

- 会话编排状态以 SQLite/checkpoint 为准，不应只依赖 UI 缓存状态。
- 新增终态行为时，必须保持可恢复性与“用户显式确认写回”的边界。
