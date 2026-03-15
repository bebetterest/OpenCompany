# 技术路线

## 当前实现基线

OpenCompany 当前落地为面向任意项目目录的本地优先 multi-agent 运行时：

- root 协调者与 worker 共享同一循环模型，并由角色策略做差异约束。
- worker 在隔离可写工作区执行；root 完成时先暂存改动，再由用户显式 apply。
- 工具调用以 `tool_run` 一等状态持久化，支持查询/等待/取消。
- 运行时状态通过 SQLite checkpoint + 追加式 JSONL（events/messages/diagnostics）持久化。
- Web UI 是主入口，TUI 作为能力对齐的回退入口。

## 路线原则

1. 协调者保持“组织优先”，避免成为执行重心。
2. 优先采用可组合的基础原语，避免硬编码流程分支堆叠。
3. 所有限制显式可配（`max_root_steps`、`max_agent_steps`、扇出、并发 worker）。
4. 副作用操作必须可控（`stage -> apply`，并支持 `undo` 备份回滚）。
5. Prompt 与文档保持双语、可版本化、目录结构确定。

## 近期演进方向

1. 对 `orchestrator.py` 中已具备边界的逻辑继续模块化拆分。
2. 进一步收紧工具动作与 finish 语义的 schema/protocol 校验。
3. 在不改编排与存储核心的前提下，基于当前“本地 + SSH 远端”路径继续扩展沙箱后端（例如 Docker）。
4. 提升 UI 在大历史场景下的可用性（diff 导航、诊断视图密度等）。
5. 加强 resume/cancel/部分失败路径的系统级测试覆盖。

## 文档约定

- 运行时/接口行为变化必须同步更新 `docs/modules/` 对应模块文档。
- 文档应围绕稳定实现边界（`orchestration`、`tools`、`workspace`、`storage`、`webui`、`tui`）组织，而不是临时重构记录。
