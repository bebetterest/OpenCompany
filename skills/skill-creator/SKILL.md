# Skill Creator

Use this skill when creating a new OpenCompany skill or adapting an imported skill to this project's rules.

## OpenCompany skill layout

Each skill should live under one of these roots:

- `<project_dir>/skills/<skill_id>/...`
- `<app_dir>/skills/<skill_id>/...`

Minimum required files:

```text
skill.toml
SKILL.md
```

Optional files:

```text
SKILL_cn.md
resources/...
```

Keep bundled files under `resources/`. Do not use legacy top-level `scripts/`, `references/`, `assets/`, or `agents/` directories for new OpenCompany skills.

## Authoring rules

- Put metadata in `skill.toml`, not YAML frontmatter inside `SKILL.md`.
- Keep `SKILL.md` short and procedural. Move detailed docs, scripts, templates, and binaries into `resources/`.
- Write the trigger description in `skill.toml` clearly enough that the skill can be selected correctly.
- Add `SKILL_cn.md` when the skill is expected to be used in Chinese sessions.
- If you import an upstream Codex skill, remove or rewrite stale Codex-specific instructions before treating it as a bundled OpenCompany skill.

## `skill.toml` fields

Use a `[skill]` table:

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

Field expectations:

- `id`
  The stable machine-readable id. It must match the folder name exactly and should use letters, digits, `.`, `_`, or `-` only.
- `name`
  Short English display name for humans.
- `name_cn`
  Short Chinese display name. Do not omit it just because the skill body is mostly English.
- `description`
  The primary trigger description. Write when to use the skill and what it helps with, not a vague slogan.
- `description_cn`
  Chinese mirror of `description`.
- `tags`
  Non-empty list of short classification labels.

Rules:

- Treat all six fields as required for repository-quality skills.
- Do not rely on runtime fallback values; fill the metadata explicitly.
- Keep `description` and `description_cn` aligned with the actual `SKILL.md` body.

## Recommended process

1. Pick a short hyphen-case `skill_id`.
2. Create the folder under the target `skills/` root.
3. Add `skill.toml` with `id`, `name`, `name_cn`, `description`, `description_cn`, and `tags`.
4. Write `SKILL.md` with the smallest useful workflow.
5. Add `SKILL_cn.md` if needed.
6. Put helper files under `resources/`.
7. Run `resources/scripts/quick_validate.py <path/to/skill>` before considering the skill complete.

## Included helpers

- `resources/scripts/init_skill.py`
  Creates an OpenCompany-style skill skeleton with `skill.toml`, `SKILL.md`, optional `SKILL_cn.md`, and optional `resources/...` subdirectories.
- `resources/scripts/quick_validate.py`
  Checks that a skill follows the OpenCompany layout and flags legacy Codex packaging patterns.

## Content guidelines

- Prefer short workflows over long theory.
- Reference files only when they materially reduce repetition.
- Use scripts for deterministic or repetitive operations.
- Keep examples realistic and task-shaped.
- Avoid auxiliary files that do not help the agent execute the task.
