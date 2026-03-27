# Message Stream Mapping

This document is a quick map of where each visible runtime block comes from in CLI/TUI/Web UI.

## Canonical storage and APIs

- Per-agent messages: `sessions/<session_id>/<agent_id>_messages.jsonl`
  - Source for model-visible conversation (`user` / `assistant` / `tool` roles).
  - Each record includes `step_count` (agent-side runtime step at write time) so live surfaces can place
    fallback/system messages into the correct step group.
- Runtime events: `sessions/<session_id>/events.jsonl`
  - Source for operational telemetry and lifecycle events.
- Tool-run state: persisted `tool_runs` records (SQLite + APIs/CLI export)
  - Source for tool lifecycle (`queued/running/completed/failed/cancelled/abandoned`).

## Surface-level mapping

| Surface | Main conversation panel source | Hidden from conversation panel | Tool lifecycle display |
|---|---|---|---|
| Web UI `Agents` | `/api/session/{id}/messages` replay | all runtime extra entries (`*_preview`, `*_extra`) | Web UI `Tool Runs` tab (`/api/session/{id}/tool-runs`) |
| TUI `Agents` | `orchestrator.list_session_messages(...)` replay | all runtime extra entries (`*_preview`, `*_extra`) | TUI `Tool Runs` tab |
| CLI `messages --include-extra` | message page from `list_session_messages(...)` | `llm_token`, `llm_reasoning`, `tool_call_started`, `tool_call`, `tool_run_submitted`, `tool_run_updated` | CLI `tool-runs` / `tool-run-metrics` |

## Runtime event routing summary

- `llm_token`, `llm_reasoning`
  - Persisted in `events.jsonl`.
  - Not rendered in live agent conversation panels.
- `tool_call_started`, `tool_call`, `tool_run_submitted`, `tool_run_updated`
  - Persisted in `events.jsonl`.
  - Used to refresh Tool Runs data.
  - Not rendered in live agent conversation panels.
- `shell_stream`
  - Persisted in `events.jsonl`.
  - Hidden in live agent conversation panels (still available in logs/CLI extras).
- `control_message`, `protocol_error`, `sandbox_violation`, `agent_completed`
  - Persisted in `events.jsonl`.
  - Hidden in live agent conversation panels (kept for diagnostics/activity/CLI extras).

## Practical debugging path

1. Conversation mismatch: inspect `*_messages.jsonl` first (`opencompany messages ...`).
2. Tool execution mismatch: inspect `tool-runs`/`tool-run-metrics`.
3. Runtime anomaly (timeouts/errors/control): inspect events (`events.jsonl` or CLI `--include-extra`).

## Step grouping rule in live surfaces

- Primary sequence heuristic:
  - `assistant` message: starts/advances the current step.
  - `tool` / `user` message: attaches to the previous assistant step.
- `step_count` (when present) is used as a floor to avoid regressing behind runtime step progress.
