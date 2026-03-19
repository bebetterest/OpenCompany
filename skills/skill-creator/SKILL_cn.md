# Skill 创建器

当你要创建新的 OpenCompany skill，或者把导入的旧 skill 改造成符合本项目规则的版本时，使用这个 skill。

## OpenCompany skill 结构

skill 应放在以下任一目录下：

- `<project_dir>/skills/<skill_id>/...`
- `<app_dir>/skills/<skill_id>/...`

最少需要：

```text
skill.toml
SKILL.md
```

可选：

```text
SKILL_cn.md
resources/...
```

新的 OpenCompany skill 不要再使用顶层 `scripts/`、`references/`、`assets/`、`agents/` 这些旧布局；都应收敛到 `resources/` 下。

## 编写规则

- 元数据写在 `skill.toml`，不要写到 `SKILL.md` 的 YAML frontmatter 中。
- `SKILL.md` 只保留最必要的流程说明。
- 详细文档、脚本、模板、二进制资源放到 `resources/`。
- `skill.toml` 里的 `description` / `description_cn` 要清楚描述触发场景。
- 如果预计会在中文 session 中使用，补上 `SKILL_cn.md`。
- 导入旧 Codex skill 后，要主动删改不再适用的 Codex 专用说明。

## `skill.toml` 字段规范

推荐固定写成 `[skill]` 表：

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

字段含义：

- `id`
  稳定的机器可读标识，必须和目录名完全一致，只使用字母、数字、`.`、`_`、`-`。
- `name`
  面向人的英文显示名，保持简短。
- `name_cn`
  中文显示名；不要因为正文主要是英文就省略。
- `description`
  最重要的触发描述，要写清楚“什么时候用”和“帮助做什么”，不要写空泛口号。
- `description_cn`
  `description` 的中文镜像。
- `tags`
  非空的短标签列表，用于分类与检索。

规则：

- 对仓库内维护的 skill，以上六个字段都视为必填。
- 不要依赖运行时 fallback，元数据要显式写全。
- `description` / `description_cn` 要和 `SKILL.md` 的真实内容保持一致。

## 建议流程

1. 选一个简短的连字符 `skill_id`。
2. 在目标 `skills/` 根目录下创建 skill 文件夹。
3. 写 `skill.toml`。
4. 写 `SKILL.md`，必要时再补 `SKILL_cn.md`。
5. 把辅助资源放到 `resources/`。
6. 用 `resources/scripts/quick_validate.py <path/to/skill>` 做检查。

## 内置辅助脚本

- `resources/scripts/init_skill.py`
  生成 OpenCompany 风格的 skill 骨架。
- `resources/scripts/quick_validate.py`
  检查 skill 是否仍带有旧布局或缺失关键文件。
