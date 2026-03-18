# Persistence and Observability Module

## Scope

Persistence and observability layers include:

- SQLite storage (`opencompany/storage.py`)
- Structured/event/message/diagnostic loggers (`opencompany/logging.py`)
- runtime path management (`opencompany/paths.py`)

## SQLite Data Model

Primary tables:

- `sessions`: session metadata, status, summaries, config snapshot
- `agents`: agent graph nodes, lineage, completion fields
- `events`: structured runtime events
- `tool_run_timeline_events`: write-time projected tool-run lifecycle rows keyed by source event id
- `tool_run_timeline_backfills`: per-session marker for one-time legacy timeline projection backfill
- `checkpoints`: serialized runtime snapshots
- `pending_actions`: pending agent queue markers
- `tool_runs`: persisted tool execution lifecycle
- `steer_runs`: persisted steer lifecycle (`waiting`/`completed`/`cancelled`)

This schema is the durable source of truth for resume.

## JSONL Streams

Per session:

- `events.jsonl`: runtime status/activity stream
- `<agent_id>_messages.jsonl`: message-first conversation source
- optional `debug/<agent_id>__<module>.jsonl`: LLM debug request/response tracing (scoped per agent+module)

Global:

- `diagnostics.jsonl`: cross-layer diagnostics (CLI/TUI/Web/runtime)

## Message-First Reconstruction

Agent views should primarily reconstruct conversation from `*_messages.jsonl`.

Runtime events complement message logs with:

- stream previews (`llm_token`, `llm_reasoning`) for telemetry/logging (not shown in live agent panels)
- tool-run lifecycle transitions for Tool Runs views
  - new sessions project `tool_call_started` / `tool_call` / `tool_run_submitted` / `tool_run_updated` into `tool_run_timeline_events` at event-write time
  - legacy sessions lazily backfill that projection once on first tool-run detail read, then serve detail timelines from the projection table instead of rescanning session-wide events
  - explicit session clone rebuilds the same projection from cloned events up front, so cloned sessions do not pay a second first-detail backfill
- steer-run lifecycle transitions (`steer_run_submitted`, `steer_run_updated`) for Steer Runs views
- shell/protocol/control/sandbox diagnostics

## Checkpoints and Resume

Checkpoint payload includes:

- session state
- all agent states
- workspace serialization
- pending agents
- pending tool runs
- root loop index
- pending child-summary injection map

Context import restores this state, normalizes active agents to `paused`, and cancels queued/running tool runs owned by those agents.
Continue (`resume(session_id, instruction)`) then appends one root user message and re-enters the run loop.

## Export and Inspection Surfaces

CLI exposes:

- `opencompany run <task>` / `opencompany resume <session_id> "<instruction>"` (interactive terminals render a compact live panel; non-interactive output remains plain text)
  - optional `--preview-chars N` limits per-field live preview length (default `256`)
  - optional `--sandbox-backend <name>` overrides `[sandbox].backend` for that invocation only
  - `run` also accepts `--model <model>` and `--root-agent-name <name>`; `resume` also accepts `--model <model>`
- `opencompany export-logs <session_id>`
- `opencompany export-logs <session_id> --export-path /tmp/session-export.json`
- `opencompany messages <session_id> ...`
- `opencompany tool-runs <session_id> ...`
- `opencompany tool-run-metrics <session_id> [--export]`
- `opencompany tool-run-metrics <session_id> --export --export-path /tmp/tool-run-metrics.json`

The live CLI status panel combines runtime events (session lifecycle transitions + latest per-agent activity), storage snapshots (`sessions`/`agents`), `tool_runs` aggregation (running/queued/failed counts), and per-agent `*_messages.jsonl` metadata (latest message preview + output-token totals) to surface current session + agent state without full TUI/Web detail. If panel content exceeds one terminal screen, CLI auto-paginates and rotates pages every `5s`, and also supports manual page switching via `=`/`+` (next) and `-` (previous); once a manual page is chosen, it stays pinned until content shrinks back to one page.

Web UI mirrors these through `/api/session/*` endpoints.
Session export now includes both `tool_runs` and `steer_runs`, plus `tool_run_metrics` and `steer_run_metrics`.
