# UI Surfaces Module

## Scope

OpenCompany has two local UI surfaces:

- Web UI (primary): FastAPI backend + static SPA client
- TUI (fallback): Textual application

Both operate against the same orchestrator/runtime model.

## Web UI Surface

Backend entry: `opencompany/webui/server.py` + `opencompany/webui/state.py`

Major API groups:

- bootstrap/configuration: `/api/bootstrap`, `/api/launch-config`, `/api/sessions`
- execution control: `/api/run`, `/api/interrupt`
- sandbox terminal launch: `/api/terminal/open`
- remote workspace validation: `/api/remote/validate`
- observability: `/api/session/{id}/events|messages|tool-runs|tool-runs/metrics|tool-runs/{tool_run_id}|steers|steer-runs|steer-runs/metrics|steer-runs/{steer_run_id}/cancel`
- project sync: `/api/session/{id}/project-sync/status|preview|apply|undo`
- config editing: `/api/config`, `/api/config/meta`, `/api/config/save`
- event stream: WebSocket `/api/events` (batched)

Execution semantics:

- `/api/run` starts a new session when launch config provides `project_dir`.
- New-session launch config also carries `session_mode` (`direct` / `staged`), defaulting to `direct`.
- New-session launch config may carry `remote` (SSH target + remote dir + auth policy) and request-only `remote_password`.
- Remote workspace is accepted only when `session_mode=direct`; `staged + remote` is rejected.
- For password auth sessions, request-time `remote_password` is used on `/api/run`, `/api/terminal/open`, and `/api/remote/validate`.
- `/api/run` runs inside an existing session when launch config provides `session_id`, reactivates that session, and appends a fresh root agent for the new run.
- When an existing session is loaded, Web UI resolves its persisted workspace mode, shows it as locked, and ignores any mode override attempts.
- When setup/reconfigure loads an existing remote session, Web UI validates remote runtime first if selected backend is `anthropic`; backend `none` skips this pre-check.
- When `/api/run` is submitted while that session is already running, runtime immediately appends a fresh root agent to the live session and schedules it alongside existing active agents.
- For live-session root append, Web UI keeps the current in-memory agent graph and consumes incremental WebSocket updates; it avoids full `/api/session/{id}/events` replay so parent/child links and cancelled states are not transiently overwritten.
- `/api/session/{id}/steers` keeps normal enqueue semantics for active sessions; user steer submissions are tagged with a user source actor and share the same persisted steer-run path as agent-tool steering. When the target session is inactive and no other session is running, Web UI auto-continues that session and asks runtime to reactivate the steered agent before loop scheduling.
- selecting session in setup/reconfigure triggers context import only (no auto-run).
- selecting project in setup/reconfigure switches to new-session mode and clears volatile runtime views (`Overview`/`Agents` live stream/tool-run timelines) so stale data from the previously loaded session is not shown.
- in `direct` mode, `Diff` is disabled and `Apply` / `Undo` controls are unavailable because changes are already live in the target project.

Web UI-specific capabilities:

- native project/session directory pickers; when a native picker is unavailable (for example in forwarded web-only environments), Web UI falls back to an in-app directory-browser modal backed by `/api/directories` for both project and session selection
- setup supports choosing local directory vs remote SSH workspace in `direct` mode; for remote SSH, clicking `Validate & Create` runs remote validation and immediately saves launch config on success (no separate "Use Remote Directory" step)
- control-bar `Terminal` button that directly launches a system terminal window rooted at the active session workspace (persistent interactive shell)
  - launch command is fail-closed (`exec`): no fallback host-shell after backend terminal command startup
  - terminal edits occur in the same root workspace and are included in `Diff`/project-sync views
- control-bar task input uses an auto-growing multiline textarea (`rows` 1-8 with overflow scrolling after max height)
  - default prefilled task follows locale (`en`/`zh`) and switches with locale while the field remains untouched by user edits
- control-bar model input (single-line) is shown below the task input
  - default value comes from `opencompany.toml` (`[llm.openrouter].model`)
  - user can override it per run/continue; submitted value is forwarded to runtime and applied to root/worker LLM calls for that execution
- control-bar exposes an optional root-agent-name input; when non-empty, `/api/run` forwards it and runtime uses it as the base root agent name (still deduplicated in-session)
- `Agents`/`Workflow` views display per-agent model labels sourced from persisted agent metadata
- `Agents` views (Web/TUI) now show context-compression runtime metrics per agent:
  - `compression_count`
  - `current_context_tokens/context_limit_tokens`
  - `usage_ratio`
  - latest compacted range
- `Agents` live view includes role filter (`all`/`root`/`worker`) and keyword search (name/id/instruction/summary)
- tabbed views (`Overview`, `Workflow`, `Agents`, `Tool Runs`, `Steer Runs`, `Diff`, `Config`)
- workflow graph zoom/pan and agent detail focus
  - toolbar includes an `Origin` action to reset pan/scroll position back to graph origin
- workflow graph uses depth-aligned tree layout (parent centered over child span) for clearer structure readability
- workflow graph panel supports full-screen expand/collapse view while keeping the same live graph state and zoom/pan interactions
- structured `Overview` insight card (status KPIs, latest summary/message, recent activity list)
  - agent status KPIs now split `cancelled` and `terminated`; no `waiting` agent bucket
- per-row `Tool Runs` detail dialog with lifecycle timeline (`tool_call_started`, `tool_call`, `tool_run_submitted`, `tool_run_updated`) and payload inspection
  - detail fetch uses `/api/session/{id}/tool-runs/{tool_run_id}` and keeps polling while the modal is open, so running `shell` runs show live accumulated `stdout/stderr`
- per-agent full-row `Steer` button on live agent cards with a built-in compose overlay (no system prompt/confirm dialog; submit acts as confirmation) and a `Steer Runs` panel with filter/group/metrics/search
- `Steer Runs` rows and related event text prioritize showing the steer source actor (`from`) and keep the raw source channel (`via`) as secondary detail
- `Steer Runs` rows are rendered as multi-line cards: target/source/channel/created time are separated from message content, and successful runs show the inserted delivery step
- `Steer Runs` target/source actor labels prefer `name (id)` when agent names are available, and fall back to id-only labels
- `Steer Runs` group mode now includes `source` (in addition to `agent`/`status`) and supports toolbar search by run id/target/source/message
- per-agent `Terminate` button on live agent cards; one click terminates the target agent subtree and cancels related queued/running tool runs
- live agent cards expose click-to-copy chips for both agent name and agent ID
  - waiting steer rows expose `Cancel`
  - cancel is state-checked by backend (`waiting` can cancel, `completed` cannot)
- config disk re-read metadata checks
- `Agents` stream rendering policy:
  - Live agent panels are message-only: they render entries reconstructed from persisted messages.
  - Runtime extra streams (`*_preview`, `*_extra`) are not rendered in `Agents`.
  - Streaming previews (`llm_token`, `llm_reasoning`) are hidden from `Agents`.
  - Tool call steps / tool-run transitions are observed through `Tool Runs`, not `Agents`.
  - On `session_finalized`, agent cards preserve an existing terminal status (`cancelled`/`terminated`/`failed`) instead of force-overwriting to `completed`.
  - Message rendering: LLM kinds (`thinking`, `reply`, `response`) use Markdown; other message kinds (including tool message content) render as escaped plain text, with JSON pretty-print when parseable.
  - Tool call arguments and tool return payloads are rendered as labeled multi-line blocks with nested JSON indentation; UI formatting does not add extra truncation.
  - When context compaction is recorded, compressed step ranges render as a single "compressed block (step A-B)" card while preserving global step count semantics.

## TUI Surface

Entry: `opencompany/tui/app.py`

Current tab model:

- `Workflow + Log`
- `Agents`
- `Tool Runs`
- `Steer Runs`
- `Diff`
- `Config`

TUI exposes run/interrupt, setup-based session loading, project sync actions, and config editing as a terminal fallback path.
New sessions default to `direct` mode in setup; users can switch to `staged` before choosing the project directory.
In `direct` mode setup, users can choose local workspace or remote SSH workspace (target/dir/auth/known-hosts); `staged` disables remote selection.
For remote SSH setup, clicking `Validate & Create` performs remote validation (SSH target/dir/dependency checks) and immediately creates the launch config if validation succeeds.
Loaded sessions keep their original workspace mode locked.
When `Run` is used on an existing (non-active) session, runtime appends a fresh root agent for that run rather than reusing the previous root, then switches the session back to active.
When `Run` is pressed again while the same session is running, runtime immediately appends another fresh root agent to the live session and schedules it with existing active agents.
It also provides a `Terminal` action from the control row that directly launches a system terminal window using the same sandbox backend/config as agent `shell` calls, with workspace root fixed to the active session workspace. Edits made there are tracked by workspace diff/project sync just like agent edits.
The control row is organized in three lines: model input + root-agent-name input + locale switch buttons (`EN` / `中文`), a task line with explicit `Task` label and multiline `TextArea` input (content-driven height expansion, min 3/max 9 rows), and run controls (`Run`, `Terminal`, `Reconfigure`, `Interrupt`).
The model input defaults from config and remains overridable per run/continue.
Agent cards/status sections display each agent's selected model from persisted metadata.
Agent cards/status sections also display context-compression metrics (`compression_count`, context token usage, usage ratio, latest compacted range).
In `direct` mode, TUI disables the `Diff` tab and `Apply` / `Undo` controls.

CLI also exposes `opencompany terminal <session_id>` and `opencompany terminal <session_id> --self-check`.
`--self-check` verifies policy parity with agent `shell` and backend-strategy enforcement (workspace write allowed; outside write expected blocked for `anthropic`, expected allowed for `none`).
Interactive CLI run/resume status panels now include a per-agent `model` field.
CLI `run`/`tui`/`ui` setup now supports remote flags for new sessions:
- `--remote-target user@host[:port]`
- `--remote-dir /abs/linux/path`
- `--remote-auth key|password`
- `--remote-key-path ...` (required when `--remote-auth key`)
- `--remote-known-hosts accept_new|strict`

TUI `Tool Runs` capabilities now include:

- grouped list rendering with run selection (`Previous` / `Next`)
- `Detail` modal for the selected run
- detail fields: overview, arguments, result, error, lifecycle timeline
- lifecycle timeline built incrementally from runtime events (`tool_call_started`, `tool_call`, `tool_run_submitted`, `tool_run_updated`)
- auto-refresh while detail modal is open when related run events arrive

TUI `Steer Runs` capabilities now include:

- live agent-card `Steer` action with input + confirm modal
- status filters (`all`/`waiting`/`completed`/`cancelled`) and group switch (`agent`/`status`)
- waiting-row `Cancel` action with backend state validation and immediate refresh
- `Steer Runs` rows show both source actor (`from`) and source channel (`via`)
- `Steer Runs` rows use a multi-line layout and explicitly show the inserted step for delivered runs
- when steering an inactive session (and no other run is active), TUI auto-continues the session and requests reactivation of the steered agent before runtime scheduling
- when steering an inactive root that is not the current session root, runtime switches execution to that steered root (other historical roots remain unchanged)
- when the steered target is non-root, auto-continue runs that focused agent branch without reactivating root in the same pass
- event-driven panel invalidation on `steer_run_submitted` / `steer_run_updated`

TUI `Agents` live cards now include:

- `Copy Name` / `Copy ID` actions (clipboard-friendly)
- `Steer` action (input + confirm modal)
- `Terminate` action (terminates target subtree and cancels related tool runs)

## State Alignment

Both UIs share core session state from orchestrator:

- session status and summaries
- agent graph updates
- tool-run lifecycle updates
- steer-run lifecycle updates
- workspace mode plus project-sync state (`disabled` for `direct`, staged lifecycle for `staged`)

Live rendering should prioritize persisted messages for conversation content, with events used for operational telemetry.
