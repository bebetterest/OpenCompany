# Skill 安装器

当需要把外部 skill 导入 OpenCompany 的项目目录或全局目录时，使用这个 skill。

## 推荐安装位置

- 项目级：`<project_dir>/skills/<skill_id>/...`
- 全局级：`<app_dir>/skills/<skill_id>/...`

除非用户明确要求共享安装，否则优先导入到项目自己的 `skills/` 目录。

## 导入后的标准结构

```text
<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # 可选
  resources/...
```

## 导入时应做的规范化

- 确保存在 `skill.toml`，并规范成 OpenCompany 标准的 `[skill]` 表结构。
- 如果上游元数据不完整或仍是旧格式，也要重写为包含 `id`、`name`、`name_cn`、`description`、`description_cn`、`tags` 的完整结构。
- 将旧的顶层 `scripts/`、`references/`、`assets/` 移到 `resources/` 下。
- 移除不再使用的 `agents/` 元数据目录。
- 检查 `SKILL.md`，把仍然依赖 Codex 专用能力的说明改掉。

## 导入时的 `skill.toml` 规范

导入后的 skill 应至少具备：

```toml
[skill]
id = "<folder-name>"
name = "..."
name_cn = "..."
description = "..."
description_cn = "..."
tags = ["..."]
```

规则：

- `id` 必须和最终目录名一致。
- 如果上游缺少元数据，就把这六个字段都补齐，不要依赖运行时 fallback。
- 如果上游已有元数据但字段不全，或不是 `[skill]` 结构，也要重写成标准 `[skill]` 结构。
- 如果上游只有英文元数据，可以先用 `name` 填充 `name_cn`，并给 `description_cn` 写一个“需要复查”的占位说明，但导入后仍应继续完善。
- 初始导入时 `tags` 可以先是 `["imported"]`，但如果这个 skill 要长期维护，就应改成更具体的标签。

导入后优先复查这些字段：

- `name_cn`
- `description`
- `description_cn`
- `tags`

## 内置辅助脚本

所有脚本都在 `resources/scripts/` 下：

- `list-skills.py`
  列出 GitHub 仓库中的候选 skill，并标记目标 OpenCompany skills 目录里是否已经存在。
- `install-skill-from-github.py`
  从 GitHub 下载 skill，复制到目标目录，重写为 OpenCompany 标准 `skill.toml` 元数据结构，并执行规范化检查。

这些脚本可能需要网络访问。如果沙箱阻止访问，应改为申请授权后重试，而不是让用户先手动下载再继续。

## 最终检查

- 确认导入后的 skill 至少包含 `skill.toml` 和 `SKILL.md`。
- 确认 `skill.toml` 中已经包含 `id`、`name`、`name_cn`、`description`、`description_cn`、`tags`。
- 辅助文件尽量都收敛到 `resources/`。
- 如果 `SKILL.md` 里还残留 Codex 专用说明，要显式提示用户继续人工清理。
