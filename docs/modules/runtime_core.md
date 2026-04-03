# Runtime Core Module

## Scope

This module describes session-level runtime behavior centered in `opencompany/orchestrator.py`:

- Session bootstrap (`run_task`), metadata load (`load_session_context`), explicit clone (`clone_session`), and continue (`resume`)
- Root/worker lifecycle coordination
- Global limits, interruption, failure handling
- Root finalization and staged project synchronization hooks

## Lifecycle Model

A session follows this high-level state machine:

1. `running`: entered by `run_task` or `resume(session_id, instruction)`
2. `completed`: set by `_finalize_root`
3. `interrupted`: set by `_mark_interrupted`
4. `failed`: set by `_mark_failed`

Agent-level lifecycle states:

- `pending`, `running`, `paused`, `completed`, `failed`, `cancelled`, `terminated`
- Workers can also surface `completion_status` (`completed`/`partial`/`failed`/`cancelled` via cancellation path)
- Session `completion_state` is result quality only (`completed`/`partial`) and is only non-null when `session.status=completed`.

## Scheduler Model

- Active roots and workers are scheduled as independent asyncio tasks inside one session-local runtime loop.
- Tool calls remain blocking for the calling agent only, except `shell` may return early with `status=running` and continue in background after inline wait timeout; sibling agents keep progressing unless they explicitly wait on each other.
- Runtime wakeups are event-driven (`spawn_agent`, steer reactivation, task completion/cancellation, pending tool-run rebuild) rather than a batch drain of `pending_agent_ids`.
- Checkpoints still store `pending_agent_ids`, but the field is now a derived snapshot of currently active workers for debugging/resume hints, not the authoritative scheduler input.

## Runtime Limits and Budgets

Limits come from `opencompany.toml` under `[runtime.limits]`:

- `max_children_per_agent`
- `max_active_agents`
- `max_root_steps`
- `max_agent_steps`

Behavioral effects:

- Root soft step threshold (`max_root_steps`): injects wrap-up reminders (interval-controlled) and keeps the session running.
- Worker soft step threshold (`max_agent_steps`): injects wrap-up reminders (interval-controlled) without forced fallback finish.
- Child fan-out cap: blocks new `spawn_agent` calls when exhausted.
- Active-agent cap: enforced through worker scheduling semaphore.

## Context Compression Runtime

Runtime context compression is controlled by `[runtime.context]`:

- `reminder_ratio` (default `0.8`): when latest prompt usage crosses this ratio, runtime injects a per-turn compression reminder.
- `keep_pinned_messages` (default `1`): preserved head message count when summary mode is active; this is a message count, not a step count.
- `max_context_tokens` (required, `> 0`): the single source of truth for context window size.
- `compression_model` (required at compression time): fixed model used only for compression.
- `overflow_retry_attempts` (default `1`): number of forced-compress + retry attempts after overflow detection.
- preflight enforcement: before each LLM call, runtime checks previous real input usage (`current_context_tokens`) and forces `compress_context` when it exceeds `max_context_tokens` (even if provider has not returned an overflow error yet).
- after any successful manual or forced compression, runtime skips that preflight forced-compress check for the entire next agent step; if that next step retries internally, all retries in that step keep skipping the preflight check.
- compression-call timeout: uses `runtime.tool_timeouts.actions.compress_context` (default `180s`).

Context-limit source:

1. `max_context_tokens`

Compression algorithm is replacement-based:

- input: `previous_summary + unsummarized_messages`
- output: `latest_summary` (overwrites previous summary, no summary concatenation)
- soft-threshold reminder messages (`root_loop_force_finalize`, worker step-limit reminders) and context-pressure reminder messages are excluded from compression input
- the current step's compression-request message is still included in compression input, even though it remains internal for normal prompt/UI assembly
- forced compression excludes the current in-flight step from compression input/range, because runtime continues that same step after compressing and the next step still needs those in-flight messages in prompt context
- if a step emits `compress_context` together with other tools, runtime executes compression after the other tools finish so their final tool results are already present in compression input
- if a step incorrectly emits `finish` together with tools, runtime still defers `finish` until after the non-terminal tools and any `compress_context` complete, so compression is not skipped by action order
- successful forced compression also writes back internal request/result trace markers; `summarized_until_message_index` stays at the last actually summarized non-current-step message so the in-flight step remains available to the following step
- metadata persistence: `context_summary`, `summary_version`, `summarized_until_message_index`, `compression_count`, `last_compacted_message_range`, `last_compacted_step_range`
- derived logs: `<agent_id>_summaries.jsonl`

Request assembly rules:

- before first summary: `system + conversation`
- after summary exists: `system + pinned_head + latest_summary + unsummarized_messages`
- messages already covered by summary are excluded from request
- internal compression-control traces are excluded from request

## Root vs Worker Boundary

Root coordinator:

- Reassesses session context, dispatches child work, monitors tool runs.
- Must not `finish` while active (`pending`/`running`) children exist.
- `finish` maps to session-level payload (`completion_state`, `user_summary`); `follow_up_needed` remains internal runtime metadata derived by orchestration.

Worker agents:

- Execute assigned work in isolated workspace.
- Return completion payload (`status`, `summary`, `next_recommendation`) via `finish`.
- On completion, worker deltas are promoted to parent workspace before root finalization.
- Each new root/worker run starts with a prefixed identity block that states the agent's own name/id and the parent agent's name/id (or explicitly states that no parent agent exists).

## Steer Run Delivery

- Runtime supports per-agent steer runs with explicit lifecycle: `waiting -> completed | cancelled`.
- UI/user steer and agent-tool `steer_agent` both enter the same `submit_steer_run(...)` pipeline.
- Each steer run records both target agent (`agent_id`) and source actor snapshot (`source_agent_id`, `source_agent_name`).
- Before each agent LLM request, runtime loads the agent's `waiting` steers in creation order.
- If that agent is currently blocked in `wait_time` or `wait_run`, a newly submitted steer ends the wait early with `end_reason=steer_received`; steer content is still consumed in the normal next-ask path.
- If a steer is submitted to a non-schedulable agent (`paused`/`completed`/`failed`/`cancelled`/`terminated`) while the session is still `running`, runtime reactivates that agent to `running` and re-enters normal session scheduling immediately.
- Runtime prepends a localized steer intro line and appends a localized source signature to stored/delivered steer content (`--- from ...` in English, `--- 来自于 ...` in Chinese).
- If a steer targets an agent currently in `completed`, runtime appends a final reminder sentence after the signature requiring a follow-up `finish` tool call after executing the new instruction.
- Each steer is consumed once:
  - CAS transition `waiting -> completed` is applied first.
  - steer content is appended to conversation as one `role=user` message per steer.
  - message log metadata records `source=steer`, `steer_run_id`, `steer_source`, `steer_source_agent_id`, `steer_source_agent_name`, and `delivered_step`.
- Cancellation only applies to `waiting` steers (`waiting -> cancelled` via CAS). `completed`/`cancelled` are terminal.

## Import and Continue Semantics

- `load_session_context(session_id)` is read-only metadata load: it returns the original persisted session row when available (checkpoint session payload is fallback), and it does not clone, import conversations, or mutate runtime state.
- `clone_session(session_id)` creates an explicit deep copy of the session directory, checkpoints, message logs, events, tool runs, steer runs, and agent rows; clone lineage is recorded through `continued_from_session_id` and `continued_from_checkpoint_seq`.
- `_import_session_context(session_id, source)` restores session + agent graph + workspaces from checkpoint, but reconstructs conversations from `*_messages.jsonl` first (checkpoint conversation is fallback only).
- During import, active agents (`pending`/`running`) are normalized to `paused`; related queued/running tool runs become `cancelled` for `source=run` and `abandoned` for `source=resume`, then a fresh checkpoint is written immediately.
- During import/resume, runnable agents are rebuilt from live agent statuses plus pending tool runs; stored `pending_agent_ids` are treated as derived metadata only.
- Interrupt path marks active agents (`pending`/`running`) as `terminated`, cancels pending tool runs, marks session `interrupted`, and persists a checkpoint.
- `continued_from_session_id` now originates only from explicit `clone_session(...)`; merely loading a session in UI/TUI/CLI no longer creates a new lineage node.
- `resume(session_id, instruction)` now requires a non-empty instruction. With default `run_root_agent=True`, it appends a new root `user` message, switches session to `running`, and continues loops from imported context.
- `resume(...)` can optionally receive `reactivate_agent_id`; when provided and the target agent is non-schedulable (`paused`/`completed`/`failed`/`cancelled`/`terminated`), runtime reactivates that agent to `running` before scheduling.
- `run_task_in_session(session_id, task)` imports context and appends a fresh root agent (new ID) for that run, updates `session.root_agent_id`, and executes that new root while preserving prior roots for history/trace separation.
- During `resume(..., run_root_agent=True)`, if `reactivate_agent_id` points to a root agent, runtime switches `session.root_agent_id` to that target root before scheduling so only the steered root branch is executed.
- `resume(...)` can optionally receive `run_root_agent=False` (with a non-root `reactivate_agent_id`) to run focused worker execution without reactivating root in that pass; runtime executes active worker branches and only finalizes session when all agents are terminal, otherwise leaves session `running`.
- UI can terminate a specific agent subtree during an active session (`terminate_agent_subtree(session_id, agent_id)`): target + descendants are marked `cancelled` (`completion_status=cancelled`), active root/worker tasks are cancelled, and related queued/running tool runs are cancelled in one action.
- Session IDs are validated as safe slugs (`[A-Za-z0-9][A-Za-z0-9_-]*`) before session path access.
- `paused` is non-schedulable and never auto-resumed by the scheduler.

## Remote Session Runtime (Direct Mode)

- Remote session metadata is persisted per session at `.opencompany/sessions/<session_id>/remote_session.json`.
- Runtime accepts remote workspace only when session mode is `direct`; `staged + remote` is rejected.
- Remote config is loaded once on session context import and cached for the session runtime.
- `remote_session.json` never stores plaintext password.
- For password auth, runtime can reuse secure local credential storage after first successful input; when OS credential backends are unavailable, an encrypted local fallback store is used.
- Session finalization/interruption/failure triggers remote runtime cleanup (SSH control state + transient cache artifacts).

## MCP Session State

- Session config can now carry `enabled_mcp_server_ids` and persisted `mcp_state` alongside skills.
- MCP server definitions come from `[mcp]` / `[mcp.servers.<id>]` in `opencompany.toml`; per-run selection is session-local and can differ from config defaults.
- Each agent uses its own MCP connections so roots/workers in different workspaces do not share roots or tool/resource caches.
- Runtime prepares MCP connections before each agent's first LLM step, keeps them alive across later steps, and closes them on agent/session shutdown.
- Resume/import preserves enabled server ids; missing/broken servers are surfaced as warnings in `session.mcp_state` instead of being silently dropped.

## Finalization and User Confirmation

When root finalizes with `completed` or `partial`:

1. Runtime stages root workspace delta into project sync state.
2. Session summary is augmented with explicit apply/undo guidance.
3. No project writeback occurs until user runs `opencompany apply <session_id>` (or UI apply action).

Undo path:

- `opencompany undo <session_id>` restores from recorded backup metadata.

## Notes for Integrators

- Session orchestration state should be considered authoritative in SQLite/checkpoints, not inferred from UI state.
- Any new terminal behavior must preserve resumability and explicit user control over project writes.
