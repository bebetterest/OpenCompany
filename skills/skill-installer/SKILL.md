# Skill Installer

Use this skill when importing external skills into an OpenCompany project or shared app directory.

## Preferred destinations

- Project-local skills: `<project_dir>/skills/<skill_id>/...`
- Shared global skills: `<app_dir>/skills/<skill_id>/...`

Prefer the project-local `skills/` directory unless the user explicitly wants a shared global installation.

## What normalization should do

Imported skills should be normalized to the OpenCompany layout:

```text
<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # optional
  resources/...
```

During import:

- Ensure `skill.toml` exists and normalize it to a canonical OpenCompany `[skill]` table.
- Rewrite incomplete or legacy metadata so `id`, `name`, `name_cn`, `description`, `description_cn`, and `tags` are always populated.
- Move legacy top-level `scripts/`, `references/`, and `assets/` into `resources/`.
- Remove stale `agents/` metadata directories.
- Review `SKILL.md` and fix instructions that still assume Codex-only behavior.

## `skill.toml` expectations during import

Every imported skill should end up with:

```toml
[skill]
id = "<folder-name>"
name = "..."
name_cn = "..."
description = "..."
description_cn = "..."
tags = ["..."]
```

Import rules:

- `id` must match the final folder name.
- If upstream metadata is missing, synthesize all six fields rather than relying on runtime fallbacks.
- If upstream metadata exists but is incomplete or not in a `[skill]` table, rewrite it into the canonical `[skill]` layout.
- If only English metadata exists upstream, it is acceptable to seed `name_cn` from `name` and `description_cn` from a review-needed placeholder, but the user should refine them afterward.
- Default imported tags may start as `["imported"]`, but they should be tightened if the skill becomes a maintained bundled skill.

Fields to review after import:

- `name_cn`
- `description`
- `description_cn`
- `tags`

## Helper scripts

All scripts live under `resources/scripts/`.

- `list-skills.py`
  Lists candidate skills from a GitHub repo path and annotates whether they are already present in the target OpenCompany skills directory.
- `install-skill-from-github.py`
  Downloads or sparse-checkouts skills from GitHub, copies them into the target skills directory, rewrites metadata into canonical OpenCompany `skill.toml`, and runs normalization checks.

These scripts may need network access. If the sandbox blocks them, retry with approval instead of asking the user to manually download the repo first.

## Final checks

- Confirm the imported skill now has `skill.toml` and `SKILL.md`.
- Confirm `skill.toml` contains `id`, `name`, `name_cn`, `description`, `description_cn`, and `tags`.
- Prefer placing imported support files under `resources/`.
- Warn the user if the imported `SKILL.md` still contains stale Codex-specific instructions that need manual cleanup.
