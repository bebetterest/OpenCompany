# 工具运行时模块

## 范围

工具运行时包含：

- 工具 schema 注册：`opencompany/tools/definitions.py` + `prompts/tool_definitions*.json`
- 执行器：`opencompany/tools/executor.py`
- 运行时工具辅助：`opencompany/tools/runtime.py`
- 编排集成：`orchestrator.py` 中的提交/执行/取消/等待逻辑

## 工具面

当前 root/worker 默认工具集合：

- `shell`、`compress_context`、`wait_time`
- `list_mcp_servers`、`list_mcp_resources`、`read_mcp_resource`
- `list_agent_runs`、`get_agent_run`、`spawn_agent`、`cancel_agent`、`steer_agent`
- `list_tool_runs`、`get_tool_run`、`wait_run`、`cancel_tool_run`
- `finish`

角色可用工具可通过 `[runtime.tools]` 配置，其中包括 `steer_agent_scope`（`session` / `descendants`）以及 `wait_time` 的上下限（`wait_time_min_seconds` / `wait_time_max_seconds`）。

## MCP 工具与资源表面

- helper 工具：
  - `list_mcp_servers`：列出当前 agent runtime 可见的启用/已配置 MCP servers。
  - `list_mcp_resources`：按分页列出缓存中的 MCP resources，并支持可选 `server_id` 过滤。
  - `read_mcp_resource`：读取单个具体 MCP resource URI；v1 不做 resource template 展开。
- 动态 MCP tools 会在运行时以 `mcp__<server_id>__<tool_name>__<hash>` 这种 synthetic name 注入。
- 动态 MCP tools 仍然是一等工具调用：每次调用都会生成自己的 `tool_run` 行，并返回投影后的 agent 可见结果。
- MCP resource 发现是运行时可选能力：当 server 对 `resources/list` 返回 `-32601 Method not found`（或等价错误）时，runtime 会保持该 server 已连接并保留已发现 tools，同时将 resources 计为 0，而不是把 server 视为不可用。
- 动态 MCP tool 的超时预算统一归入 `runtime.tool_timeouts.actions.mcp_tool`。
- 受 OAuth 保护的 Streamable HTTP MCP server 可通过 `opencompany mcp-login --mcp-server <id>` 或 Web UI 中的 MCP 卡片登录动作完成授权；runtime 会复用已保存的 bearer token，并在具备 refresh token 时自动刷新。
- 运行进程内会按 MCP server 串行化 OAuth refresh；多个并发 agent 会复用最新轮换出的 token，而不是竞争同一份 refresh token。
- 若启用 OAuth 的 server 在 MCP 连接初始化阶段仍持续返回 HTTP `401`（包括 `invalid_token`、登录过期或缺少 refresh token），runtime 会自动清理该 server 的本地 OAuth 记录，强制下一次走干净重登链路。
- 对于在 URL 查询参数里暴露浏览器登录标记（例如 `?login`）的 hosted provider，可以直接按原地址配置；runtime 在 streamable HTTP 传输请求前会自动移除该查询标记。
- MCP OAuth 行为支持按 server 单独配置；对于要求更严格的 hosted provider，可覆盖 `oauth_authorization_prompt`、`oauth_use_resource_param` 等细节。
- 若某个 hosted server 配置在 `.../mcp` URL 上，且初次 Streamable HTTP 连接初始化失败，runtime 会在真正报错前自动重试同级的 `.../sse` 端点。
- MCP tool/resource 输出会先做脱敏与尺寸截断，再返回给 agent 或持久化到 `tool_run.result`。

## 协议原则

agent 可见工具返回采用精简投影协议：

- 不注入拼接型 summary 字符串
- 结构化字段只保留决策所需信息
- 低价值运行时噪声会被移除；仅在确有决策价值时返回输入相关字段

运行时持久化仍保留完整信息以支持回放与调试：

- `tool_run.result` 存储内部原始结果
- orchestrator 在写入 tool 消息前，将原始结果投影成 agent 可见协议

## 逐工具协议

1. `wait_time`
- 输入：`seconds`（必须位于 `[runtime.tools].wait_time_min_seconds`..`[runtime.tools].wait_time_max_seconds` 范围内，默认 `10`..`60`）
- 成功输出：`wait_time_status=true`
- 若等待中的 agent 收到新的 steer 并提前结束等待，成功输出会包含 `end_reason=steer_received`
- 失败输出：`wait_time_status=false`，并可带 `timed_out`、`timeout_seconds`、`error`

2. `compress_context`
- 输入：无参数
- 输出：`compressed`、`reason`、`summary_version`、`message_range`、`step_range`、`context_tokens_before`、`context_tokens_after`、`context_limit_tokens`
- 压缩被禁用、配置缺失或无可压缩消息时可返回 `error`
- 作用域仅限当前 agent（不支持跨 agent 压缩）
- 工具定义约束：调用 `compress_context` 时应单独调用，不要与其他工具同轮混用；只有运行时为兼容模型输出而兜底时才会处理混用场景
- `compress_context` 的工具调用/控制痕迹会标记为 internal，不再进入后续 LLM 请求
- 超时预算可通过 `runtime.tool_timeouts.actions.compress_context` 配置（默认 `180s`）

3. `list_agent_runs`
- 输入：`status`、`limit`、`cursor`
- 输出：`agent_runs_count`、`agent_runs`、`next_cursor`、`has_more`
- 行字段：`id`、`name`、`role`、`status`、`created_at`、`summary_short`、`messages_count`
- 状态过滤接受 `string|array`，并按 agent 状态白名单校验（`pending|running|paused|completed|failed|cancelled|terminated`）

4. `get_agent_run`
- 输入：`agent_id`、`messages_start`、`messages_end`
- 消息切片语义：`[messages_start, messages_end)`，其中 `messages_end` 为排他上界
- `messages_start/messages_end` 支持负数倒序下标（例如 `-1` 表示最后一条消息）
- 不传切片参数时，默认返回最后 1 条消息
- 运行时软注入提醒消息（例如上下文使用告警、root/worker 软步数提醒）会在切片前先被忽略，因此返回的下标/数量都是基于过滤后的可见消息列表
- 单次最多返回 5 条消息（messages 通常较长，避免大量拉取）
- 非法范围输入会返回明确错误（下标越界，或归一化后 `end < start`）
- 输出：`agent_run` 概览 + `messages`
- 当请求切片被 5 条上限截断时，输出会额外包含 `warning` 与 `next_messages_start`
- `messages` 每个条目仅保留：`content`、`reasoning`、`role`、`tool_calls`、`tool_call_id`
- `agent_run` 字段：`id`、`name`、`role`、`status`、`created_at`、`parent_agent_id`、`children_count`、`step_count`

5. `spawn_agent`
- 输入：`name`、`instruction`
- 输出：`tool_run_id`、`child_agent_id`

6. `cancel_agent`
- 输入：`agent_id`、可选 `recursive`（默认 `true`）
- 成功输出：`cancel_agent_status=true`
- 失败输出：`cancel_agent_status=false`，并可带 `error`

7. `steer_agent`
- 输入：`agent_id`、`content`
- 成功输出：`steer_agent_status=true`、`steer_run_id`、`target_agent_id`、`status`
- 失败输出：`steer_agent_status=false`，并可带 `configured_scope`、`error`
- runtime 会拒绝 self-steer；被拒绝时不会创建 steer run
- 目标可达范围受 `[runtime.tools].steer_agent_scope` 控制

8. `list_tool_runs`
- 输入：`status`、`limit`、`cursor`
- 输出：`tool_runs_count`、`tool_runs`、`next_cursor`、`has_more`
- 状态过滤校验：`queued|running|completed|failed|cancelled|abandoned`

9. `get_tool_run`
- 输入：`tool_run_id`、`include_result`（默认 `false`）
- 输出：`tool_run` 概览
- 当 `include_result=true` 时，概览里包含完整 `result`
- 对 `shell` run，概览还会包含 `stdout`/`stderr`；当状态为 `running` 时，这两项来自运行中累计的流式输出快照

10. `wait_run`
- 输入：`tool_run_id` 或 `agent_id` 二选一
- schema 兼容性说明：为避免 provider 侧工具参数校验拒绝，此互斥约束仅写在字段描述中（不再使用顶层 `oneOf`/`not`），并由运行时校验
- 成功输出：`wait_run_status=true`
- 若等待中的 agent 收到新的 steer 并提前结束等待，成功输出会包含 `end_reason=steer_received`
- 失败输出：`wait_run_status=false`，并可带 `timed_out`、`timeout_seconds`、`error`
- 对 agent 的等待仅在终态算成功；`paused` 不算成功

11. `cancel_tool_run`
- 输入：`tool_run_id`
- 输出：`final_status`、`cancelled_agents_count`
- 失败时可带 `error`
- 终态 run 上取消是 no-op；已完成的 `spawn_agent` run 不会取消 child agent

12. `finish`
- 输入：`status`、`summary`、`next_recommendation`（仅 worker）
- 输出：`accepted`（失败时可带 `error`）
- root 的 `finish.status` 仅允许 `completed|partial`；worker 保持 `completed|partial|failed`
- `follow_up_needed` 已从工具输入移除，且不会投影到 tool message
- `submitted_summary` 不会投影到 tool message

## 分页

列表型工具共享游标分页策略：

- 请求字段：`limit` + `cursor`
- 返回字段：`next_cursor` + `has_more`
- 默认 `limit`：`[runtime.tools].list_default_limit`（默认 20）
- 范围：`1..[runtime.tools].list_max_limit`（默认上限 200）

游标编码策略：

- `list_agent_runs` 使用不透明 offset cursor
- `list_tool_runs` 使用不透明 `(created_at, id)` cursor 以保持时间线稳定排序

## Tool Run 生命周期

每次通过校验的工具 action 都会写成持久化 `tool_run`：

- 标识：`toolrun-*`
- 状态：`queued` -> `running` -> `completed|failed|cancelled|abandoned`
- 时间戳：`created_at`、`started_at`、`completed_at`
- 负载：arguments、原始 result、error
- 详情时间线：按 `tool_run_id` 读取投影后的生命周期行（`tool_call_started`、`tool_call`、`tool_run_submitted`、`tool_run_updated`）
  - 新 session 会在 event 追加时增量写入这些投影行
  - 旧 session 会在首次打开详情时做一次投影回填

## 执行语义

- 大多数工具按阻塞模式执行
- `shell` 使用 `[runtime.tools].shell_inline_wait_seconds`（默认 `5.0`）：若命令在阈值内未完成，会返回 `status=running`、`background=true`、`tool_run_id` 与当前 `stdout`/`stderr`，并继续后台执行
- `shell` 在同一工具契约下同时支持本地路径与远程（`direct` 模式 SSH）路径，具体执行由 `[sandbox].backend`（`anthropic`/`none`）决定
- `anthropic` 运行时会在受约束 shell/terminal 会话里默认注入 `NODE_USE_ENV_PROXY=1`，让 Node 客户端遵循 sandbox 代理路径；若命令显式传入同名环境变量，仍可覆盖默认值
- `spawn_agent` 创建 child 后立即返回（`child_agent_id` + `tool_run_id`）
- `steer_agent` 与用户/UI steer 提交共用同一套持久化 steer-run 流程
- tool schema 不提供按调用覆盖的阻塞参数

### 远程 Shell 路径（SSH，V1）

- transport backend 取决于 `[sandbox].backend`：
  - `anthropic`：SSH + 远端 `srt --settings ...` 执行
  - `none`：SSH + 远端 `/bin/bash --noprofile --norc -c ...` 执行（无约束）
- 两种 backend 都会复用会话级 SSH ControlMaster 连接；远端 settings 文件按内容 hash 复用仅适用于 `anthropic`
- host key 策略支持 `accept_new`（默认）和 `strict`
- `anthropic` 的依赖策略是 fail-closed：
  - 首次依赖准备会使用更长超时预算（`600s`），用于容忍包安装耗时
  - 若缺少 `rg`，会尝试通过 root 或 `sudo -n` 使用系统包管理器（`apt/dnf/yum/zypper/apk/pacman`）自动安装
  - 若缺少 `bwrap`/`socat`，会尝试特权自动安装 `bubblewrap`/`socat`
  - 在 apt 系统上会强制使用非交互安装环境，并在重试前自动执行 `dpkg --configure -a` + `apt-get -f install` 修复
  - 运行依赖 `Node.js >= 18`；缺失或版本过低时，会先尝试系统包安装 `nodejs`
  - 在 apt 且检测到中国 locale/timezone 提示时，会先用临时 TUNA 源安装 `nodejs`，失败后回退远端默认 apt 源
  - 若安装后的 `nodejs` 仍 `<18`，会在受支持的 apt 发行版（Debian/Ubuntu）尝试 NodeSource `node_20.x` 仓库安装
  - 若 NodeSource 不可用或安装后仍 `<18`，会回退到用户态 Node.js tarball 安装（`$HOME/.local/node-v20`，中国环境优先 TUNA `nodejs-release`，再尝试 `nodejs.org`）
  - 若缺少 `srt` 且 `npm` 也缺失，会先尝试特权安装 `npm`，再在 npm 用户态路径（`$HOME/.local`）安装 `srt`
  - 依赖准备完成后会执行 `srt --help` 启动自检，用于提前发现 Node/运行时不兼容
  - 任一安装失败仍会按 fail-closed 直接终止 run/validate，并返回明确依赖错误
  - 运行时会执行 bubblewrap namespace 能力预检；若 namespace 创建被禁止（`Operation not permitted` / `kernel.unprivileged_userns_clone=0`），会提前失败并返回明确提示
  - 安装状态会通过 shell stream 输出，前缀为 `[opencompany][remote-setup]`
- password auth 使用 `sshpass` 的一次性本地临时文件；每条命令后会删除
- `none` backend 在 shell 执行路径不强制 sandbox 文件/网络策略（`network_policy`/`allowed_domains` 在运行时不生效）

## 校验与指标

- `validate_finish_action(...)` 在执行前校验角色字段组合
- `validate_wait_time_action(...)` 与 `validate_wait_run_action(...)` 校验等待工具约束（`wait_time` 上下限来自 `[runtime.tools]`）
- `tool_run_metrics(...)` 输出总量、状态分布、失败/取消率、耗时分位与直方图、按工具/agent 聚合
