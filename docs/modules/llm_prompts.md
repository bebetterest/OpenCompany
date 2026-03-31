# LLM and Prompts Module

## Scope

This module describes:

- OpenRouter integration (`opencompany/llm/openrouter.py`)
- protocol normalization (`opencompany/protocol.py`)
- prompt/tool-definition loading (`opencompany/prompts.py`, `prompts/`)

## OpenRouter Streaming Path

`OpenRouterClient.stream_chat(...)` sends:

- `model`, `messages`, `temperature`, `max_tokens`
- `tools`, `tool_choice`, `parallel_tool_calls`
- streaming enabled (`stream=true`)

The SSE parser merges:

- assistant content tokens
- reasoning fragments + details
- incremental tool call parts (name/arguments)
- usage/provider/finish metadata

Retry behavior exists for:

- transport/API failures before the first streamed event (`max_retries`, exponential backoff + jitter; retries all HTTP error status codes `4xx/5xx`, plus retryable transport errors, and respects server retry hints like `Retry-After`/`RateLimit-Reset` when present)
- empty stream responses under guarded conditions
- empty protocol responses with no JSON object (`empty_response_retries` in `AgentRuntime`)

Runtime event logging for observability:

- `llm_retry`: unified retry event for API/network/empty-stream/empty-protocol retries, including `status_code`, `status_text`, per-source attempt counters, delay, retry reason, and unified fields (`overall_retry_attempt`, `overall_retry_category`)
- `llm_request_error`: emitted when an OpenRouter request fails and bubbles up, including HTTP status metadata when available
- `context_overflow_retry`: separate overflow-specific retry path (forced compression + retry), independent from `llm_retry` accounting

## Protocol Normalization

Action extraction order:

1. if tool calls are present, normalize from tool-call payload
2. otherwise extract JSON object from assistant content
3. normalize into runtime action list

Invalid or empty protocol responses are handled by runtime control-message + fallback finish flow.

## Prompt and Tool Definition Loading

`PromptLibrary` loads role/runtime assets from `prompts/`:

- agent prompts: `root_coordinator*.md`, `worker*.md`
- runtime message templates: `runtime_messages*.json`
- tool schemas: `tool_definitions*.json`

Locale behavior:

- `zh` uses `_cn` variants
- fallback defaults to English assets when localized files are missing

## Skill Prompt Augmentation

When a session has enabled skills:

- runtime stores a `skills_catalog` inside each agent's metadata
- `ContextAssembler.system_prompt()` appends an `Enabled Skills` block after the role prompt
- that block includes:
  - skill bundle root
  - manifest path
  - per-skill doc path / localized doc path
  - any drift or missing-source warnings

The appended section is informational only: skills do not register new tools or change tool schemas.
The role prompts also explain how to use that section: root should incorporate it into planning/delegation and pass exact paths downstream, while worker should read the referenced docs first and use `shell` for any file/script/binary inspection.

## Role and Locale Coupling

- System prompt is role-specific (`root` / `worker`).
- Tool definition descriptions are locale-aware for consistent model/tool UX.
- Model selection can vary by role (`model`, `coordinator_model`, `worker_model`).
- UI run controls can override model per execution; when provided, the same selected model is applied to both root and worker calls for that run/continue.
- Each agent persists its selected model in metadata (`metadata.model`) during runtime; runtime events expose it as `agent_model` for CLI/TUI/WebUI rendering.

## Coordination Guardrails in Prompts

- Root prompt enforces non-overlapping child scopes, dependency-aware assignment with explicit `child_agent_id` references, and no root-side duplicate execution of delegated scopes.
- Root prompt also requires precise child scope contracts when spawning agents: explicitly state what each child can do and cannot do; work assigned to other agents is treated as out of scope unless reassigned.
- Root prompt explicitly prefers `steer_agent` for course corrections or extra constraints on an existing agent, instead of spawning a new overlapping child.
- Root prompt also states that inter-agent messaging/replies should go through `steer_agent`, that new user messages are authoritative and must be followed strictly, and that messages from other agents must be analyzed before application.
- Root prompt enforces spawn-task-bound action scope and a no-touch rule for referenced files/content unless modification permission is explicitly stated.
- Root prompt also states that when an `Enabled Skills` block is present, it should be treated as a reusable-resource hint and relevant skills should be preferred when useful; children should receive exact listed paths, and the materialized skill bundle stays read-only unless the user explicitly asks otherwise.
- Root prompt requires active progress checks for running children, allows explicit `wait_time` / `wait_run` usage, and mandates dependency-chain termination when a required child is terminated.
- Root prompt requires post-child validation and cleanup before downstream use, with targeted re-delegation for follow-up and local handling only for trivial edits.
- Root prompt instructs user handoff with analysis summary when completion is near-impossible (for example no viable path or effort estimate beyond 24 hours).
- Root prompt states that ended agents should have `finish` summary/feedback details, and recommends checking the last message via `get_agent_run(agent_id)`.
- Worker prompt enforces strict scope compliance to avoid cross-agent interference in parallel execution.
- Worker prompt also forbids self-initiated extra additions for out-of-scope specified content.
- Worker prompt requires the same precise scope contracts when it spawns children: each child instruction must list allowed vs forbidden work, and work handled by other agents stays out of scope unless reassigned.
- Worker prompt also prefers `steer_agent` when an already-running agent only needs correction or additional constraints.
- Worker prompt also states that inter-agent messaging/replies should go through `steer_agent`, that new user/parent-agent messages are authoritative and must be followed strictly, and that messages from other non-parent agents require the worker's own analysis and judgment.
- Worker prompt also enforces the same no-touch rule: referenced files/content remain read-only unless explicit modification permission is granted.
- Worker prompt also states that when an `Enabled Skills` block is present, it should be treated as a reusable-resource hint and relevant skills should be preferred when useful: read the referenced docs first, use listed paths exactly, inspect/execute skill scripts or binaries only via `shell`, and keep the materialized skill bundle read-only unless explicitly allowed.
- Worker prompt mirrors the same anti-overlap, dependency-ID propagation, no-parent-duplicate-execution, dependency-chain termination, and mid-flight wait/check guardrails when it creates child agents.
- Worker prompt requires dependency-aware execution against referenced agent outputs, post-child validation/cleanup, and explicit blocked-state summaries when completion is not feasible in the current environment.
- Worker prompt also repeats the ended-agent `finish` summary/feedback expectation and the `get_agent_run(agent_id)` last-message lookup pattern.
