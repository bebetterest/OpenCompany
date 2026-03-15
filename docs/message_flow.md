# Message Flow

This document describes the current runtime protocol: unified `finish`, persisted `tool_run` lifecycle, and one loop model for both root and worker agents.

## Message channels

OpenCompany uses four channels in parallel:

1. `agent.conversation`
   Replayable history for one agent. This is the only history re-sent to the model.
2. API tool fields
   `tools`, `tool_choice`, and `parallel_tool_calls` are sent as API fields.
3. Runtime events
   Structured events (`agent_prompt`, `agent_response`, `tool_call`, `tool_run_*`, `agent_completed`, etc.) for UI/debugging.
4. Per-agent message logs
   `sessions/<session_id>/<agent_id>_messages.jsonl` stores reconstructed messages plus response metadata.

## Message-first live reconstruction

Live `Agents` views (TUI/Web UI) should rebuild primary conversation content from per-agent message logs, not from runtime events.

- Primary stream source: `*_messages.jsonl` (`user` / `assistant` / `tool` messages).
- Runtime event role: status/activity/workflow updates and operational diagnostics.
- Tool call steps and tool-run status transitions are tracked from runtime events but rendered in dedicated Tool Runs views (CLI `tool-runs`, UI `Tool Runs` tab), not in live agent conversation panels.
- Live agent panels render message-derived entries only (including tool message content from role=`tool` messages).
- Runtime extra stream entries (`*_preview`, `*_extra`) are hidden from live agent panels.
- Live rendering policy for visible message entries: only LLM kinds (`thinking`, `reply`, `response`) use Markdown; other message kinds are escaped plain text, with JSON pretty-print when parseable.

## Unified per-turn request assembly

Every `AgentRuntime.ask()` call assembles:

```python
messages = [
    {"role": "system", "content": system_prompt},
    *agent.conversation,
]
```

Where:

- `system_prompt` is selected by role (`root` or `worker`) and locale.
- `tools` is selected by role from configurable runtime tool registry.
- Both roles share the same terminal tool name: `finish`.
- `agent_prompt` runtime events expose both views from the same snapshot:
  - `request_messages`: exact payload sent to the LLM API
  - `messages`: conversation-only view (`request_messages` without the leading `system` message)

## Tool runs as first-class state

Every action becomes a persisted `tool_run` record:

- `id` (`tool_run_id`)
- `session_id`, `agent_id`
- `tool_name`, `arguments`
- lifecycle timestamps (`created_at`, `started_at`, `completed_at`)
- `status` (`queued`, `running`, `completed`, `failed`, `cancelled`)
- raw `result` / `error`

The runtime keeps two views of one tool execution:

- storage/debug view: full raw `tool_run.result`
- agent-visible view: projected compact payload appended into conversation

This separation keeps observability detail without polluting model context.

## Agent-visible tool payload contract

Agent-visible tool payloads are normalized to:

- no synthesized summary string
- minimal structured fields needed for next decisions
- no low-value argument echo in normal success responses

List-style tools (`list_agent_runs`, `list_tool_runs`) use a unified pagination shape:

- request: `limit`, `cursor`
- response: `next_cursor`, `has_more`
- default `limit` is controlled by `[runtime.tools].list_default_limit` (default 20)

Tool-run inspection behavior:

- `list_tool_runs`: overview rows only
- `get_tool_run`: overview by default (for `shell`, includes `stdout`/`stderr` snapshot while running); include raw `result` only when `include_result=true`
- `wait_run`: status-only contract (`wait_run_status` plus optional timeout/error markers)
- `cancel_tool_run`: minimal contract (`final_status`, `cancelled_agents_count`)

## Tool-call semantics

- Most tool calls are handled in blocking mode. `shell` may return early with `status=running`, `background=true`, and `tool_run_id` when inline wait is exceeded; the command keeps running in the background.
- `spawn_agent` creates the child and returns immediately with `child_agent_id` and `tool_run_id`.
- There is no per-call blocking override field in tool schemas.

## Spawn semantics

`spawn_agent` behavior:

1. Runtime creates child agent and child workspace.
2. Runtime returns `tool_run_id` and `child_agent_id` in the same turn.
3. Spawn tool run is completed in the same step after creation.
4. Child executes in its own loop.
5. If a spawned branch is no longer needed, parent/root can call `cancel_agent(agent_id)`.
6. Child completion summaries are not auto-injected into parent context; parent/root should inspect child status explicitly.

## Finish semantics

Both roles call `finish`; runtime validates by role:

- root: status enum includes `interrupted`; `next_recommendation` is disallowed
- worker: status enum excludes `interrupted`; `next_recommendation` is available

`finish` schema does not include a blocking field.
Runtime rejects `finish` if the agent still has unfinished child agents.
Runtime rejects invalid role-field combinations before execution.

## Step limits and forced summary

- Worker soft threshold: when worker `step_count` reaches `max_agent_steps`, runtime appends wrap-up reminder control messages (interval-controlled) and allows additional turns.
- Root soft threshold: when root `step_count` reaches `max_root_steps`, runtime appends wrap-up reminder control messages (interval-controlled) and allows additional turns.

## Checkpoint and resume

Checkpoint includes:

- session state
- agent states
- workspace states
- pending agent IDs
- pending tool run IDs
- root loop index

On resume, runtime restores checkpointed state (session/agents/workspaces/pending IDs) and reconstructs spawn run ↔ child linkage when needed.

## Recommended collaboration pattern

```json
{
  "actions": [
    {"type": "spawn_agent", "instruction": "Inspect module A"},
    {"type": "list_agent_runs", "limit": 20}
  ]
}
```

Then:

```json
{
  "actions": [
    {"type": "wait_time", "seconds": 10},
    {"type": "list_agent_runs", "limit": 20},
    {"type": "finish", "status": "completed", "summary": "Integrated child result."}
  ]
}
```
