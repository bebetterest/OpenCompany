# 持久化与可观测模块

## 范围

本模块覆盖：

- SQLite 存储层（`opencompany/storage.py`）
- 结构化/事件/消息/诊断日志（`opencompany/logging.py`）
- 运行路径管理（`opencompany/paths.py`）

## SQLite 数据模型

核心表：

- `sessions`：会话元数据、状态、总结、配置快照
- `agents`：agent 图节点、父子关系、完成字段
- `events`：结构化运行时事件
- `checkpoints`：运行快照序列化
- `pending_actions`：待处理 agent 队列标记
- `tool_runs`：工具执行生命周期
- `steer_runs`：引导执行生命周期（`waiting`/`completed`/`cancelled`）

恢复语义以该持久层为准。

## JSONL 流

会话级：

- `events.jsonl`：运行状态/活动流
- `<agent_id>_messages.jsonl`：message-first 主对话源
- 可选 `debug/<agent_id>__<module>.jsonl`：LLM 请求/响应调试追踪（按 agent+module 分文件）

全局：

- `diagnostics.jsonl`：CLI/TUI/Web/runtime 跨层诊断

## Message-First 重建

Agent 实时对话应优先由 `*_messages.jsonl` 重建。

runtime events 作为补充，承载：

- `llm_token` / `llm_reasoning` 预览遥测（不在 agents 实时面板展示）
- Tool Runs 视图所需的 tool-run 生命周期迁移
- Steer Runs 视图所需的 steer-run 生命周期迁移（`steer_run_submitted`、`steer_run_updated`）
- shell/protocol/control/sandbox 诊断告警

## Checkpoint 与恢复

checkpoint 负载包含：

- session 状态
- 全量 agent 状态
- workspace 序列化
- pending agents
- pending tool runs
- root loop 索引
- pending child-summary 注入映射

导入上下文会恢复上述状态，并将活跃 agent 规范为 `paused`，同时取消这些 agent 关联的 queued/running tool run。
继续执行（`resume(session_id, instruction)`）会先给 root 追加一条 user 消息，再重新进入主循环。

## 导出与查询面

CLI：

- `opencompany run <task>` / `opencompany resume <session_id> "<instruction>"`（交互式终端显示简洁动态状态面板；非交互输出保持纯文本）
  - 可选 `--preview-chars N` 用于限制各字段实时预览长度（默认 `256`）
  - 可选 `--sandbox-backend <name>` 仅覆盖本次调用的 `[sandbox].backend`
  - `run` 还支持 `--model <model>` 与 `--root-agent-name <name>`；`resume` 还支持 `--model <model>`
- `opencompany export-logs <session_id>`
- `opencompany export-logs <session_id> --export-path /tmp/session-export.json`
- `opencompany messages <session_id> ...`
- `opencompany tool-runs <session_id> ...`
- `opencompany tool-run-metrics <session_id> [--export]`
- `opencompany tool-run-metrics <session_id> --export --export-path /tmp/tool-run-metrics.json`

CLI 动态状态面板采用“runtime events + 存储快照 + 聚合统计”的组合方式：用事件追踪会话生命周期与各 agent 最近活动时间，用 `sessions`/`agents` 快照展示当前会话与 agent 状态，用 `tool_runs` 聚合 running/queued/failed 计数，并从各 agent 的 `*_messages.jsonl` 元数据提取最新消息预览与累计 output token，总体避免展开为 TUI/Web 级别细节。若面板内容超过一屏，CLI 会自动分页并按 `5s` 轮换页面，同时支持 `=`/`+`（下一页）与 `-`（上一页）手动切页；手动切页后会固定在所选页，直到内容回落为单页。

Web UI 在 `/api/session/*` 下提供对应能力。
会话导出现在同时包含 `tool_runs` 与 `steer_runs`，以及 `tool_run_metrics` 与 `steer_run_metrics`。
