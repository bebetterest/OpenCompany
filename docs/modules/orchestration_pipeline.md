# Orchestration Pipeline Module

## Scope

This module covers orchestration flow implemented by:

- `opencompany/orchestration/agent_loop.py`
- `opencompany/orchestration/agent_runtime.py`
- `opencompany/orchestration/context.py`
- `opencompany/orchestration/messages.py`
- Runtime integration points in `opencompany/orchestrator.py`

## Loop Engine (`AgentLoopRunner`)

`AgentLoopRunner.run(...)` is role-agnostic and drives one reusable cycle:

1. ask agent for actions
2. execute action batch
3. stop if finish payload arrives
4. on step exhaustion, inject soft wrap-up reminders (no hard-forced finish)

Result envelope:

- `finish_payload`
- `interrupted`
- `step_limit_reached`

## Runtime Scheduling

`opencompany/orchestrator.py` now treats each active agent as its own runnable unit:

- roots and workers run in separate runtime tasks
- task wakeups are event-driven (`spawn_agent`, steer reactivation, completion/cancellation, pending tool-run rebuild)
- tool execution is still blocking per caller, but there is no session-wide batch barrier that waits for sibling agents before continuing
- `pending_agent_ids` remains a checkpoint/debug snapshot, not the authoritative scheduler queue

## Agent Runtime (`AgentRuntime`)

`AgentRuntime.ask(...)` performs:

- role-aware prompt resolution (`root` vs `worker`)
- tool schema injection from runtime registry
- one-shot request assembly reused by both event logging and LLM API calls
- streaming token/reasoning event logging
- assistant message persistence (with timing and response metadata)
- protocol parsing into normalized actions

Empty-protocol retry behavior:

- retries only when response is structurally empty and configured retries remain
- otherwise injects runtime invalid-response control message and falls back to a failure-style `finish`

## Context Assembly and Storage

`ContextAssembler` decides:

- system prompt by role and locale
- tool list by role (from config + prompt-defined schemas)
- request message packing (`system` + conversation)

MCP integration adds two runtime context layers:

- agent-specific MCP prompt block appended after skills, summarizing enabled servers, connection warnings, and discovered counts
- agent-specific tool surface expansion, which merges built-in tools with helper MCP tools and dynamic MCP tool definitions discovered from connected servers

`ContextStore` handles:

- append conversation/tool-result messages
- persistence hooks back to storage/message loggers

## Control Message Paths

Runtime control messages are injected for non-happy paths, including:

- invalid protocol responses
- worker soft-step wrap-up reminders
- unfinished-children finish rejection context
- root soft-step wrap-up reminders
- cancelled-child summary request prompts (for spawn-branch cancellation)

## Child Summary Delivery

Completed child summaries are not auto-injected into parent context:

- parent/root must explicitly inspect progress and results via tool-run tools
- cancellation path still attempts one best-effort cancelled-child summary in cancel output

## Resume Semantics in the Pipeline

On context import / continue:

- checkpoint reconstructs agent/session/workspace graph
- conversations are rebuilt from persisted `*_messages.jsonl` first (checkpoint conversation is fallback only)
- active agents are normalized to `paused`, queued/running tool runs owned by those agents are cancelled, and a new checkpoint is written
- continue requires `resume(session_id, instruction)`: append one root user message, then re-enter the event-driven runtime loop
