# Documentation Index

OpenCompany documentation is organized as a layered map aligned with the current runtime implementation.

## Recommended Reading Path

1. `README.md`: setup, CLI/Web usage, and operational safety model.
2. `docs/technical_route.md`: technical direction and evolution priorities.
3. `docs/architecture.md`: system architecture and runtime execution chain.
4. `docs/modules/*.md`: subsystem-level references for daily engineering.
5. `docs/message_flow.md`: protocol/message details for model-visible history and UI streams.
6. `docs/message_stream_map.md`: surface-by-surface mapping of what each runtime block reads from.

## Document Map

### Entry and architecture

- `docs/technical_route.md` / `docs/technical_route_cn.md`
- `docs/architecture.md` / `docs/architecture_cn.md`
- `docs/message_flow.md` / `docs/message_flow_cn.md`
- `docs/message_stream_map.md` / `docs/message_stream_map_cn.md`

### Module references (`docs/modules/`)

- `runtime_core.md`: session lifecycle, limits, root/worker boundary.
- `orchestration_pipeline.md`: loop engine, context assembly, forced summary behavior, and MCP-aware prompt/tool injection.
- `tool_runtime.md`: tool registry, executor, `tool_run` lifecycle semantics, dynamic MCP tool/resource surfaces, and remote SSH sandbox transport behavior.
- `workspace_sync.md`: `direct` / `staged` workspace modes, local/remote root selection in `direct`, fork/merge, diff artifacts, staged apply/undo.
- `skills.md`: skill source discovery, project materialization, resume replacement semantics, and prompt/runtime integration.
- `persistence_observability.md`: SQLite, JSONL logs, checkpoints, diagnostics.
- `llm_prompts.md`: OpenRouter streaming path and prompt/tool-definition loading.
- `ui_surfaces.md`: Web UI and TUI setup flows (local vs remote workspace in `direct`), session mode selection/locking, MCP selection state, APIs, and Tool/Steer Runs panels.
- `testing_debugging.md`: test matrix, validation commands, debugging workflow.

Each module doc has a synchronized Chinese mirror (`*_cn.md`) with the same section structure.

## Documentation Maintenance Rules

- Keep English and Chinese mirrors synchronized by structure and core facts.
- Update this index when adding/removing docs or changing navigation.
- Prefer implementation-first wording: document behavior that already exists in `src/opencompany`.
- When architecture changes, update `README.md`, `docs/architecture*.md`, and affected module docs in one pass.
