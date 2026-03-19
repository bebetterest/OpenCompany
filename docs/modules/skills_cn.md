# Skills 模块

## 范围

Skills 相关行为由以下实现：

- `opencompany/skills.py`
- `opencompany/orchestrator.py` 中的 skill 发现与物化逻辑
- `opencompany/cli.py`、`opencompany/webui/server.py`、`opencompany/webui/state.py` 中的 CLI / Web 入口

## 来源模型

OpenCompany 支持两层 skill 来源：

- 项目源：`<project_dir>/skills/<skill_id>/...`
- 全局源：`<app_dir>/skills/<skill_id>/...`

解析规则：

- 同一 `skill_id` 时，项目源覆盖全局源
- `.opencompany_skills/` 下的运行时副本永远不会被当作 discover 源
- 远程 `direct` 会话也会从 `<remote_dir>/skills/` 发现项目源 skills

## Skill 包结构

每个 skill 目录按以下结构校验：

```text
<source-root>/<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # 可选
  resources/...      # 可选；可包含文本、脚本或二进制文件
```

校验规则：

- 目录名与 `skill.toml` 中的 `id` 必须一致
- `SKILL.md` 为必需文件
- symlink 会被忽略，不会复制到运行时副本中
- 允许脚本与二进制资源，但它们只作为文件存在，不会自动变成工具

## 如何添加 Skill

若要添加项目级 skill，请创建：

```text
<project_dir>/skills/<skill_id>/
```

若要添加全局 skill，请创建：

```text
<app_dir>/skills/<skill_id>/
```

最少需要的文件：

```text
skill.toml
SKILL.md
```

最小元数据示例：

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

创建目录后，可通过以下命令确认是否能被发现：

```bash
opencompany skills --project-dir /path/to/target
```

如果项目源与全局源里存在相同的 `skill_id`，项目源优先。

## Session 物化

每个 session 会持久化：

- `enabled_skill_ids`
- `skill_bundle_root`（`.opencompany_skills/<session_id>`）
- `skills_state`（上次物化的 entry 元数据与告警）

项目/工作区内的物化目标：

- `.opencompany_skills/<session_id>/<skill_id>/...`
- `.opencompany_skills/<session_id>/manifest.json`

`manifest.json` 记录：

- 当前启用的 skill ids
- 来源类型与来源路径
- 物化后的 bundle/doc 路径
- 每个文件的 `sha256`、`size`、`mode`、`is_binary`、`is_executable`
- 告警记录

## Run / Resume / Clone 语义

`run`：

- 解析请求的 skill ids
- 发现来源描述
- 把选中的 skills 复制到 session bundle root
- 持久化生成后的 `skills_state`

`resume`：

- 当传入 `--skill` / `enabled_skill_ids` 时，替换整个 session 级 skill 集合
- 未传入时，保留已有 `enabled_skill_ids`
- 在任何 agent 继续前重建整个 `.opencompany_skills/<session_id>` 目录
- 被禁用的 skill 副本目录会立即删除

`clone`：

- 保留 `enabled_skill_ids`
- 将 `skill_bundle_root` 重写为新的 session id
- 在首次 resumed / imported 执行时重建物化路径

## 漂移与缺源

重建策略：

- 若来源内容相对上次物化发生变化，OpenCompany 会按最新来源重建，并记录 `content_drift` 告警
- 若某个 skill id 在项目源和全局源里都缺失，则跳过该 skill，并记录 `missing_source` 告警
- 缺失 skill 不会导致整个 session 失败，但会从当前有效物化集合中移除

## 运行时集成

root 与 worker agents 都会在 metadata 中收到 `skills_catalog`。

Prompt 行为：

- `ContextAssembler.system_prompt()` 会追加 `Enabled Skills` 区块
- 该区块包含 bundle root、manifest 路径、启用的 skill ids、本地化文档路径与告警
- prompt 会明确要求 agent 把这些物化文件视为只读运行时资源

执行行为：

- skills 不会新增 agent 可见工具
- skill 中的脚本或二进制文件只能通过现有 `shell` 工具按需查看或执行

## 工作区与同步语义

`.opencompany_skills/<session_id>` 必须保留在 session 工作区中，这样 workers 才能读取同一份 bundle。

但它会被排除在 staged project-sync 流程之外：

- 工作区 diff 计算
- per-worker diff artifact
- worker 向父工作区的增量提升
- staged project-sync preview
- staged apply / undo

这类排除发生在 project-sync 逻辑里，而不是通过全局 ignored-path 列表处理。
