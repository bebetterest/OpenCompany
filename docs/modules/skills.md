# Skills Module

## Scope

Skills behavior is implemented by:

- `opencompany/skills.py`
- skill discovery/materialization helpers in `opencompany/orchestrator.py`
- CLI/Web entry points in `opencompany/cli.py`, `opencompany/webui/server.py`, and `opencompany/webui/state.py`

## Source Model

OpenCompany supports two skill source tiers:

- project source: `<project_dir>/skills/<skill_id>/...`
- global source: `<app_dir>/skills/<skill_id>/...`

Resolution rules:

- project source overrides global source on the same `skill_id`
- runtime materialized copies under `.opencompany_skills/` are never treated as discoverable sources
- remote `direct` sessions also discover project-source skills from `<remote_dir>/skills/`

## Skill Package Layout

Each skill directory is validated as:

```text
<source-root>/<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # optional
  resources/...      # optional; may contain text, scripts, or binary files
```

Validation rules:

- directory name and `skill.toml` `id` must match
- `SKILL.md` is required
- symlinks are ignored and not copied into runtime bundles
- scripts and binary resources are allowed; they are treated as files, not tools

## Adding a Skill

To add a project skill, create:

```text
<project_dir>/skills/<skill_id>/
```

To add a global skill, create:

```text
<app_dir>/skills/<skill_id>/
```

Minimum required files:

```text
skill.toml
SKILL.md
```

Minimal metadata example:

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

After creating the directory, verify discovery with:

```bash
opencompany skills --project-dir /path/to/target
```

If the same `skill_id` exists in both places, the project source wins over the global source.

## Session Materialization

Every session stores:

- `enabled_skill_ids`
- `skill_bundle_root` (`.opencompany_skills/<session_id>`)
- `skills_state` (last materialized entry metadata + warnings)

Materialization target inside the project/workspace:

- `.opencompany_skills/<session_id>/<skill_id>/...`
- `.opencompany_skills/<session_id>/manifest.json`

`manifest.json` records:

- enabled skill ids
- source type/path
- materialized bundle/doc paths
- per-file `sha256`, `size`, `mode`, `is_binary`, `is_executable`
- warning records

## Run / Resume / Clone Semantics

`run`:

- resolves requested skill ids
- discovers source descriptors
- copies selected skills into the session bundle root
- persists the resulting `skills_state`

`resume`:

- when `--skill` / `enabled_skill_ids` is provided, it replaces the session-wide skill set
- when omitted, it keeps the previously enabled skill ids
- rebuilds the whole `.opencompany_skills/<session_id>` tree before any agent continues
- removes bundle directories for disabled skills immediately

`clone`:

- keeps `enabled_skill_ids`
- rewrites `skill_bundle_root` to the new session id
- rebuilds materialized paths on first resumed/imported execution

## Drift and Missing Sources

Rebuild policy:

- if source content changed since the last materialization, OpenCompany rebuilds from the latest source and records a `content_drift` warning
- if a skill id is missing from both project/global sources, it is skipped and recorded as `missing_source`
- missing skills do not fail the whole session; they are removed from the active materialized set

## Runtime Integration

Root and worker agents receive a `skills_catalog` in agent metadata.

Prompt behavior:

- `ContextAssembler.system_prompt()` appends an `Enabled Skills` block
- the block includes bundle root, manifest path, enabled skill ids, localized doc paths, and warnings
- agents are told to treat the materialized files as read-only runtime assets
- root prompt also tells the coordinator to incorporate relevant skills into planning/delegation and to steer children with exact doc/file paths from that block
- worker prompt also tells executors to read the referenced skill docs first and to inspect or execute skill scripts/binaries only through `shell`

Execution behavior:

- no new agent-visible tool is added for skills
- scripts or binaries inside a skill may only be inspected/executed through the existing `shell` tool

## Workspace and Sync Semantics

`.opencompany_skills/<session_id>` must stay inside session workspaces so workers can read the same bundle.

But it is excluded from staged project-sync flows:

- workspace diff computation
- per-worker diff artifacts
- worker-to-parent promotion
- staged project-sync preview
- staged apply / undo

This exclusion is applied in project-sync logic, not through the global ignored-path list.
