# LLM 与 Prompt 模块

## 范围

本模块说明：

- OpenRouter 接入（`opencompany/llm/openrouter.py`）
- 协议归一化（`opencompany/protocol.py`）
- prompt/tool 定义加载（`opencompany/prompts.py`、`prompts/`）

## OpenRouter 流式链路

`OpenRouterClient.stream_chat(...)` 发送：

- `model`、`messages`、`temperature`、`max_tokens`
- `tools`、`tool_choice`、`parallel_tool_calls`
- `stream=true`

SSE 解析会合并：

- assistant 内容 token
- reasoning 片段与 details
- 递增 tool call 片段（name/arguments）
- usage/provider/finish 元数据

重试覆盖：

- 首个流式事件到达前的传输/API 失败（`max_retries` + 指数退避与抖动；覆盖全部 HTTP 错误状态码 `4xx/5xx` 以及可重试传输错误，并在服务端提供 `Retry-After`/`RateLimit-Reset` 时按提示等待）
- 受保护条件下的空流响应

可观测性事件日志：

- `llm_retry`：OpenRouter 触发重试时记录，包含 `status_code`、`status_text`、重试次数、等待时长与重试原因
- `llm_request_error`：OpenRouter 请求失败并上抛时记录；可用时包含 HTTP 状态元数据

## 协议归一化

动作提取顺序：

1. 若有 tool calls，优先从 tool-call 负载归一
2. 否则从 assistant content 提取 JSON 对象
3. 归一为 runtime action 列表

无效或空协议响应会进入控制消息 + fallback finish 路径。

## Prompt 与工具定义加载

`PromptLibrary` 从 `prompts/` 加载：

- agent prompts：`root_coordinator*.md`、`worker*.md`
- 运行时消息模板：`runtime_messages*.json`
- 工具 schema：`tool_definitions*.json`

locale 行为：

- `zh` 优先 `_cn` 文件
- 缺失时回退英文

## Skill Prompt 增补

当 session 启用了 skills 时：

- 运行时会把 `skills_catalog` 写入每个 agent 的 metadata
- `ContextAssembler.system_prompt()` 会在角色 prompt 后追加 `Enabled Skills` 区块
- 该区块包含：
  - skill bundle root
  - manifest 路径
  - 每个 skill 的文档路径 / 本地化文档路径
  - 漂移或缺源告警

这个附加区块只提供上下文信息：skills 不会注册新工具，也不会修改 tool schema。

## 角色与语言耦合

- 系统提示词按角色（`root` / `worker`）选择。
- 工具描述按 locale 输出，保证模型可见工具定义与 UI 语言一致。
- 模型可按角色拆分配置（`model`、`coordinator_model`、`worker_model`）。
- UI 运行控制支持“按次覆盖模型”；设置后该次 run/continue 会对 root 与 worker 统一使用该模型。
- 每个 agent 在运行时会把所选模型持久化到 metadata（`metadata.model`）；runtime 事件通过 `agent_model` 透出，供 CLI/TUI/WebUI 渲染展示。

## Prompt 编排护栏

- Root prompt 强制子任务范围不重叠、依赖关系分配时显式带上 `child_agent_id`，并禁止 root 对已委派范围重复执行。
- Root prompt 还要求创建subagent 时给出精确范围契约：明确该subagent 能做什么和不能做什么；已由其他agent 负责的工作除非重新分配，否则都视为越界。
- Root prompt 还明确要求：若已有 agent 只需要纠偏或补充约束，应优先使用 `steer_agent`，而不是再创建一个范围重叠的新subagent。
- Root prompt 还要求所有 agent 间消息/回复都通过 `steer_agent` 发送；新的用户消息属于权威指令必须严格遵循，而来自其他agent 的消息则必须先分析再决定如何采用。
- Root prompt 还要求在 spawn 子任务时严格按子任务指令范围行动，并对参考文件/内容执行“未明确允许修改即只读”的约束。
- Root prompt 要求对运行中的子任务进行中途巡检，可明确使用 `wait_time` / `wait_run`，且在终止关键子任务时必须联动终止依赖链分支。
- Root prompt 要求子任务完成或终止后先验收与清理，再进入下游；需要继续推进时做有针对性的再拆分，仅对极小编辑由 root 直接处理。
- Root prompt 规定在几乎不可完成（如无可行路径或预计超过24小时）时，向用户交接分析总结而非盲目继续。
- Root prompt 强调已结束 agent 应带有 `finish` 的总结与反馈信息，并建议通过 `get_agent_run(agent_id)` 查看最后一条消息获取。
- Worker prompt 强制严格遵守分配范围，避免并行多agent场景下的互相干扰。
- Worker prompt 也禁止对指定范围外内容擅自额外添加。
- Worker prompt 也要求其在创建subagent 时使用同样的精确范围契约：每条子指令都应明确可做/不可做，其他agent 正在负责的工作默认不可做，除非重新分配。
- Worker prompt 也明确要求：若已有运行中的 agent 只需要纠偏或补充约束，应优先使用 `steer_agent`。
- Worker prompt 还要求所有 agent 间消息/回复都通过 `steer_agent` 发送；新的用户/父agent 消息属于权威指令必须严格遵循，而来自其他非父级agent 的消息则需要 worker 自行分析判断后再使用。
- Worker prompt 同样要求参考文件/内容默认只读：未明确给出修改权限时，不得修改或删除。
- Worker prompt 在其创建子任务时也遵循同级护栏：防重叠分配、依赖 `child_agent_id` 传递、禁止父级重复执行、终止依赖链联动、中途巡检与等待。
- Worker prompt 要求基于依赖agent产出推进，先做子任务结果验收与清理，并在当前环境不可完成时提交清晰阻塞分析与下一步建议。
- Worker prompt 也强调已结束 agent 的 `finish` 总结/反馈约束，以及通过 `get_agent_run(agent_id)` 查看最后一条消息的做法。
