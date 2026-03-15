# 工作区同步模块

## 范围

工作区与项目同步由以下实现：

- `opencompany/workspace.py`
- `opencompany/orchestrator.py` 中的 project sync 逻辑

## 工作区模式

每个 session 都会持久化一个工作区模式：

- `direct`（新会话默认）：root 与 worker agents 共享目标项目真实目录，改动立即生效。
- `staged`：先在 session 工作区里编辑，root 收尾时再生成待确认的项目 diff。

工作区模式只能在新建 session 时选择。后续 reconfigure/load/resume 都会保留最初模式并锁定显示。

## Remote Direct 工作目录（SSH，V1）

- 远程工作目录仅支持 `direct` 模式。
- `staged + remote` 会在 UI setup 层和后端校验层都被拒绝。
- 远程目标格式为 `user@host[:port]`；远程工作目录必须是 Linux 绝对路径。
- 会话级远程元数据持久化在 `.opencompany/sessions/<session_id>/remote_session.json`。
  - 存储字段：`ssh_target`、`remote_dir`、`auth_mode`、`identity_file`、`known_hosts_policy`、`remote_os`
  - 明文密码不会写入 `remote_session.json`
- 远程配置在会话上下文导入时读取一次，运行期走内存复用（runtime/terminal 共用）。

## 工作区拓扑

每个 session 目录下：

- `snapshots/`：`staged` 模式的基线快照（`root_base` 与 child base）
- `workspaces/`：可写 child 工作区
- `diffs/`：每 agent 的 diff artifact（`<agent_id>.json`）

`staged` 模式下的 root 工作区创建：

- 将项目复制到 `snapshots/root_base`
- 再克隆到 `snapshots/root` 作为 root 工作视图

`direct` 模式下的 root 工作区创建：

- 将配置的实时目标目录作为 `root.path`（本地路径或由 remote config 映射的远程路径）
- 将 `root.base_snapshot_path` 也指向同一个实时目标目录
- 不再创建 `snapshots/root_base` 和 `snapshots/root`

child 工作区创建：

- `staged`：从父工作区快照 fork 到隔离的 `workspaces/<agent_id>`
- `direct`：worker 复用 `workspace_id="root"`，直接写入目标项目目录

## 变更跟踪

`WorkspaceManager` 计算：

- `added`、`modified`、`deleted`
- 文本/二进制感知的文件 diff 预览
- 含文件列表、patch 与 patch 元数据的 diff artifact JSON

忽略路径包含内部运行噪声（`.git`、`.opencompany`、缓存、`.env*` 等）。
symlink 在全链路都会被排除（快照、树遍历、搜索、diff、apply/undo 同步）。

## 向上增量提升

当 worker 在 `staged` 模式下以 `completed` 或 `partial` 完成时：

1. 将 worker 增量应用到父工作区
2. 诊断日志记录提升文件数量
3. worker 仍保留自身 diff artifact

若提升失败：

- worker 完成状态降级为 `failed`
- summary/recommendation 会附带恢复建议

在 `direct` 模式下，不再有向上提升步骤，因为 worker 改动已经直接落在共享 root 工作区里。同时也不会生成稳定的 per-worker diff artifact。

## Stage / Apply / Undo 模型

`staged` 模式下，root 收尾时先暂存项目同步状态：

- 状态文件：`project_sync.json`
- 备份目录：`project_sync_backups/`
- 常见状态流转：`pending -> applied -> reverted`

`apply`：

- 将暂存改动写入目标项目
- 写入回滚所需备份清单

`undo`：

- 按备份元数据恢复文件
- 尽可能删除 apply 时新增的文件

`direct` 模式会完全禁用 project sync：

- project-sync status 返回 `disabled`
- diff preview/apply/undo API 会快速报错
- Web UI 与 TUI 会禁用 `Diff` / `Apply` / `Undo`

## 远程运行时清理

针对 remote `direct` 会话：

- 每条命令后：删除一次性本地密码临时文件（password auth 路径）
- 会话结束/中断/失败：关闭本地 SSH 控制 socket 状态
- 远端缓存 GC：清理临时 `exec_*.sh`、`*.lock`、`*.pid`，保留恢复所需的可复用 settings 文件

## 运维约束

- `staged` 会话可以在“有暂存但未 apply”状态下结束。
- `staged` 的最终写回必须由用户显式确认。
- `undo` 仅针对该 `staged` session 最近一次记录的 apply。
- `direct` 会话没有运行时回滚层；如需恢复，依赖 Git 或手工回退。
