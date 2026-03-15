# Technical Route

## Current Baseline

OpenCompany is implemented as a local-first multi-agent runtime for arbitrary project directories:

- Root coordinator and workers share one loop model, with role-specific policy checks.
- Workers execute in isolated writable workspaces; root finalization stages changes before explicit apply.
- Tool invocations are persisted as first-class `tool_runs` with query/wait/cancel support.
- Runtime state is durable through SQLite checkpoints plus append-only JSONL events/messages/diagnostics.
- Web UI is the primary surface; TUI remains a fallback surface with aligned core capabilities.

## Route Principles

1. Keep the coordinator as organizer-first, not execution-heavy.
2. Prefer minimal composable primitives over hardcoded workflow branches.
3. Keep limits explicit and configurable (`max_root_steps`, `max_agent_steps`, fan-out, active workers).
4. Keep side effects gated (`stage -> apply`, `undo` with backups) and resumability first-class.
5. Keep prompts/docs bilingual and versioned with deterministic file layout.

## Near-Term Direction

1. Continue modular extraction from `orchestrator.py` where logic is already clearly separable.
2. Tighten schema/protocol validation around tool actions and finish semantics.
3. Expand sandbox backend options beyond the current local + SSH-remote path (for example Docker) without rewriting orchestration or storage layers.
4. Improve UI scalability (large histories, richer diff navigation, denser diagnostics views).
5. Strengthen system tests for resume/cancel/partial-failure patterns.

## Documentation Contract

- Any runtime/interface behavior change must update the matching module document in `docs/modules/`.
- Docs should reference implementation seams (`orchestration`, `tools`, `workspace`, `storage`, `webui`, `tui`) instead of temporary refactor notes.
