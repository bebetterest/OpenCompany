# UI 界面模块

## 范围

OpenCompany 当前提供两套本地界面：

- Web UI（主入口）：FastAPI 后端 + 静态 SPA
- TUI（回退入口）：Textual 应用

两者共享同一 orchestrator/runtime 能力。

## Web UI

后端入口：`opencompany/webui/server.py` + `opencompany/webui/state.py`

主要 API 分组：

- 启动配置：`/api/bootstrap`、`/api/launch-config`、`/api/sessions`
- 执行控制：`/api/run`、`/api/interrupt`
- sandbox 终端拉起：`/api/terminal/open`
- 远程工作目录校验：`/api/remote/validate`
- 可观测性：`/api/session/{id}/events|messages|tool-runs|tool-runs/metrics|tool-runs/{tool_run_id}|steers|steer-runs|steer-runs/metrics|steer-runs/{steer_run_id}/cancel`
- 项目同步：`/api/session/{id}/project-sync/status|preview|apply|undo`
- 配置编辑：`/api/config`、`/api/config/meta`、`/api/config/save`
- 事件流：WebSocket `/api/events`（批量发送）

执行语义：

- 当 launch config 提供 `project_dir` 时，`/api/run` 新建会话并运行。
- 新建会话的 launch config 还会携带 `session_mode`（`direct` / `staged`），默认是 `direct`。
- 新建会话的 launch config 还可携带 `remote`（SSH 目标、远程目录、认证策略）以及仅请求态的 `remote_password`。
- 远程工作目录仅在 `session_mode=direct` 时可用；`staged + remote` 会被拒绝。
- 对 password auth 会话，请求态 `remote_password` 会用于 `/api/run`、`/api/terminal/open` 与 `/api/remote/validate`。
- 当 launch config 提供 `session_id` 时，`/api/run` 会在已有会话内执行：先重新激活该会话，再为这次运行追加一个全新的 root agent。
- 加载已有会话时，Web UI 会直接绑定原始 `session_id`，解析其持久化的 workspace mode，并以锁定只读方式展示；外部 mode 覆盖会被忽略。
- 在 setup/reconfigure 加载已有远程会话时，若当前选择 backend 为 `anthropic`，Web UI 会先执行远程运行时校验；backend 为 `none` 时跳过该预校验。
- 当会话已在运行中时再次提交 `/api/run`，runtime 会立刻在该 live session 里追加一个新的 root agent，并与当前活跃 agents 一起调度执行。
- 对运行中会话追加 root 时，Web UI 会保留当前内存中的 agent 图并只消费 WebSocket 增量事件；不会全量重放 `/api/session/{id}/events`，以避免父子关系与 cancelled 状态被瞬时覆盖。
- 对活跃会话，`/api/session/{id}/steers` 仍是正常排队语义；用户 steer 会带上用户来源 actor，并与 agent 工具 steer 共用同一条持久化 steer-run 流程。当目标会话非活跃且当前没有其他运行中会话时，Web UI 会自动继续该会话，并在进入调度前请求 runtime 重新激活被 steer 的 agent。
- 在 setup/reconfigure 中选择 session 时，只会加载持久化的 session 元数据（不会隐式 clone，也不会自动运行）。
- 在 setup/reconfigure 中选择项目目录会切换到“新会话”模式，并清空易失运行视图（`Overview`/`Agents` 实时流、tool-run 时间线），避免继续显示上一个已加载 session 的旧数据。
- 在 `direct` 模式下，`Diff` 会被禁用，`Apply` / `Undo` 也不可用，因为改动已经直接写入目标项目。

Web UI 特性：

- 原生项目/会话目录选择器；当原生目录选择器不可用（例如仅通过端口转发访问 Web UI）时，项目与会话目录选择都会自动降级为基于 `/api/directories` 的内置目录浏览弹窗
- setup 在 `direct` 模式下支持本地目录与远程 SSH 工作目录切换；远程 SSH 下点击“校验连接并创建”会先执行远程校验，成功后直接保存启动配置（不再需要单独“使用远程目录”步骤）
- 控制栏 `终端` 按钮：直接拉起系统终端窗口，根路径固定为当前活动 session workspace（持久交互）
  - 启动命令采用 fail-closed（`exec`），backend 终端命令启动后不会回落到 host shell
  - 终端内改动发生在同一 root workspace，会纳入 `Diff` / project-sync 视图
- 控制栏任务输入框改为自动增高的多行 textarea（`rows` 1-8，超过上限后内部滚动）
  - 默认会按语言（`en`/`zh`）预填任务，且在用户未手动改写该输入框时会随语言切换同步更新
- 控制栏在任务输入框下方提供单行模型输入框
  - 默认值来自 `opencompany.toml`（`[llm.openrouter].model`）
  - 每次运行/继续前可覆盖；提交值会透传到 runtime，并在该次执行中同时作用于 root/worker 的 LLM 调用
- 控制栏提供可选 root-agent-name 输入框；非空时 `/api/run` 会透传该值，runtime 以它作为 root agent 命名基底（仍保留会话内去重）
- `Agents` / `Workflow` 视图会显示每个 agent 的模型标签，数据来源于持久化 agent metadata
- session 历史恢复改为窗口化：Web UI 首先请求 `/api/session/{id}/events?limit=200&activity_only=true` 与 `/api/session/{id}/messages?tail=200&limit=200`，更早内容通过 `before` cursor 按需继续加载
- 首屏历史恢复会跳过持久化的 `llm_reasoning`、`llm_token` 与 `shell_stream`；这些内容只会在会话活跃时通过 WebSocket 实时展示
- `Agents` 视图（Web/TUI）会显示每个 agent 的上下文压缩运行指标：
  - `compression_count`
  - `current_context_tokens/context_limit_tokens`
  - `usage_ratio`
  - 最近一次压缩范围
- `Agents` 实时视图提供类别筛选（`all`/`root`/`worker`）与关键字搜索（name/id/instruction/summary）
- 标签页：`Overview`、`Workflow`、`Agents`、`Tool Runs`、`Steer Runs`、`Diff`、`Config`
- 工作流图缩放/拖拽与 agent 详情聚焦
  - 工具栏提供“回到原点”操作，用于将平移与滚动位置恢复到流程图原点
- 工作流图采用按深度对齐的树状布局（父节点按子树中轴居中），结构关系更易读
- 工作流图面板支持全屏放大/收起查看，保持同一份实时图状态与缩放/拖拽交互
- 结构化的 `Overview` 运行洞察卡片（状态 KPI、最新总结/消息、最近活动列表）
  - agent 状态 KPI 现拆分 `cancelled` 与 `terminated`，不再保留 agent `waiting` 统计桶
- `Tool Runs` 每行支持详情弹窗，可查看生命周期时间线（`tool_call_started`、`tool_call`、`tool_run_submitted`、`tool_run_updated`）和 payload
  - 详情数据通过 `/api/session/{id}/tool-runs/{tool_run_id}` 拉取，弹窗打开时持续轮询；因此运行中的 `shell` 可实时查看累计 `stdout/stderr`
  - 持久化详情时间线现在来自投影化的 tool-run detail API；旧 session 仅在首次打开详情时做一次懒回填，不会每次都重新扫描整段 session events
- 实时 agent 卡片提供整行 `Steer` 按钮，并使用界面内引导弹框（不再使用系统 prompt/confirm；点击提交即确认），并新增 `Steer Runs` 面板（过滤/分组/统计/搜索）
- `Steer Runs` 行与相关事件文本优先展示 steer 来源 actor（`from`），原始 source 通道作为次级信息（`via`）
- `Steer Runs` 行改为多行卡片式展示：目标、来源、通道、创建时间与消息内容分开展示，成功送达的 run 会明确显示插入到哪个步骤
- `Steer Runs` 的目标/来源 actor 标签在可用时优先显示为 `名称 (ID)`，不可用时回退为仅 ID
- `Steer Runs` 的分组模式新增 `来源`（除 `按 Agent`/`按状态` 外），并支持在工具栏按 run id/目标/来源/消息内容搜索
- 实时 agent 卡片新增 `终止` 按钮；点击后会终止目标 agent 子树，并取消关联 queued/running tool runs
- 实时 agent 卡片提供可点击复制的名称/ID 标签，便于快速复制 agent name 与 agent id
  - `waiting` 行提供 `取消`
  - 取消由后端做状态校验（`waiting` 可取消，`completed` 不可取消）
- 配置文件落盘后元数据校验
- `Agents` 流渲染策略：
  - `Agents` 实时面板是 message-only：仅渲染持久化 messages 重建出的条目。
  - runtime extra 流（`*_preview`、`*_extra`）不在 `Agents` 面板展示。
  - `llm_token` / `llm_reasoning` 流式预览不在 `Agents` 展示。
  - 工具调用步骤与 tool-run 状态迁移统一在 `Tool Runs` 观测，不进入 `Agents`。
  - 收到 `session_finalized` 事件时，若 agent 已是终态（`cancelled`/`terminated`/`failed`），卡片状态会保留该终态，不会强制覆盖为 `completed`。
  - message 渲染规则：LLM 类型（`thinking`、`reply`、`response`）按 Markdown；其它 message 类型（含 tool 返回内容）按转义纯文本，若可解析为 JSON 则格式化。
  - tool 调用参数与 tool 返回 payload 以“带标签的多行块”展示，嵌套 JSON 保持缩进，界面层不再额外截断。
  - 当记录到上下文压缩事件时，被压缩 step 范围会折叠为单个“压缩块（step A-B）”展示，同时保持全局 step 计数语义不变。

## TUI

入口：`opencompany/tui/app.py`

当前标签页：

- `Workflow + Log`
- `Agents`
- `Tool Runs`
- `Steer Runs`
- `Diff`
- `Config`

TUI 提供 run/interrupt、基于 setup 的会话加载、project sync 操作和配置编辑，作为终端回退路径。
新建会话在 setup 中默认使用 `direct` 模式；在选择项目目录前可切换为 `staged`。
在 `direct` 模式 setup 中可选择本地目录或远程 SSH 工作目录（target/dir/auth/known-hosts）；`staged` 会禁用远程选择。
远程 SSH setup 下点击“校验连接并创建”会执行远程校验（SSH 目标/目录/依赖检查），校验通过后立即创建启动配置。
已加载会话会保留原始 `session_id`，并锁定原始 workspace mode。
当在已有（非活跃）会话上点击 `运行` 时，runtime 会为本次执行追加一个全新的 root agent，而不是复用旧 root，并将该会话切回活跃态。
当同一会话仍在运行时再次点击 `运行`，runtime 会立刻追加新的 root agent，并与当前活跃 agents 一起调度执行。
同时在控制栏提供 `终端` 动作，直接拉起系统终端窗口，复用与 agent `shell` 调用一致的 sandbox backend/config，且工作目录固定到当前活动 session workspace。终端改动与 agent 改动一样会被 workspace diff/project sync 跟踪。
控制栏采用三行布局：第一行是模型输入 + root-agent-name 输入 + 语言切换按钮（`EN` / `中文`），第二行是带明确 `任务` 标签的多行 `TextArea` 输入（按内容自动增高，最小 3 行、最大 9 行），第三行是运行控制按钮（`运行`、`终端`、`重新配置`、`中断`）。
模型输入默认读取配置，并可按每次运行/继续自由覆盖。
Agent 卡片/状态区域会显示各 agent 的模型，来源于持久化 metadata。
Agent 卡片/状态区域也会显示上下文压缩指标（`compression_count`、上下文 token 使用、使用率、最近压缩范围）。
在 `direct` 模式下，TUI 会禁用 `Diff` 标签页以及 `Apply` / `Undo` 控件。

CLI 同时提供 `opencompany terminal <session_id>` 与 `opencompany terminal <session_id> --self-check`。
`--self-check` 会同时校验与 agent `shell` 的策略一致性，以及按 backend 的运行时语义（workspace 内可写；`anthropic` 期望 workspace 外被阻止，`none` 期望 workspace 外可写）。
交互式 CLI 的 run/resume 状态面板现包含每个 agent 的 `model` 字段。
CLI 的 `run`/`tui`/`ui` 在新建会话时支持远程参数：
- `--remote-target user@host[:port]`
- `--remote-dir /abs/linux/path`
- `--remote-auth key|password`
- `--remote-key-path ...`（`--remote-auth key` 时必填）
- `--remote-known-hosts accept_new|strict`

TUI `Tool Runs` 现支持：

- 分组列表展示，并可在列表内选中 run（`上一条` / `下一条`）
- 对当前选中 run 打开 `详情` 弹窗
- 详情字段：概览、arguments、result、error、生命周期时间线
- 持久化详情时间线由投影化的 tool-run detail 读取提供；实时更新仍会按 runtime 事件增量追加（`tool_call_started`、`tool_call`、`tool_run_submitted`、`tool_run_updated`）
- 详情弹窗打开期间，相关 run 新事件到达会自动刷新详情

TUI `Steer Runs` 现支持：

- 实时 agent 卡片 `Steer` 动作（输入 + 确认弹窗）
- 状态过滤（`all`/`waiting`/`completed`/`cancelled`）与分组切换（`agent`/`status`）
- `waiting` 行 `取消` 动作（后端状态校验 + 立即刷新）
- `Steer Runs` 行会同时展示来源 actor（`from`）和来源通道（`via`）
- `Steer Runs` 行使用多行布局，并会明确显示已送达 run 的插入步骤
- 当 steer 目标会话非活跃（且当前没有其他运行中会话）时，TUI 会自动继续该会话，并在 runtime 调度前请求重新激活被 steer 的 agent
- 当 steer 的是一个非当前 `session.root_agent_id` 的非活跃 root 时，runtime 会将执行切换到该被 steer 的 root（其他历史 root 保持不变）
- 当被 steer 目标是非 root agent 时，自动继续会以该 agent 为焦点执行，不会在同一轮同时重新激活 root
- 收到 `steer_run_submitted` / `steer_run_updated` 事件后自动标记面板脏并重渲染

TUI `Agents` 实时卡片新增：

- `复制名称` / `复制 ID` 动作（便于写入系统剪贴板）
- `Steer` 动作（输入 + 确认弹窗）
- `终止` 动作（终止目标子树并取消关联 tool runs）

## 状态对齐

两套 UI 共享的核心状态来源：

- 会话状态与总结
- agent 图更新
- tool-run 生命周期更新
- steer-run 生命周期更新
- workspace mode，以及 project-sync 状态（`direct` 下为 `disabled`，`staged` 下走完整暂存生命周期）

对话内容展示应优先使用持久化 messages，events 主要用于运行遥测。
