# Tool Runtime Module

## Scope

Tool runtime spans:

- tool schema registry: `opencompany/tools/definitions.py` + `prompts/tool_definitions*.json`
- executor: `opencompany/tools/executor.py`
- runtime helpers: `opencompany/tools/runtime.py`
- orchestration glue: tool-run submission/execution/cancel/wait logic in `orchestrator.py`

## Tool Surface

Current default tool set for root and worker:

- `shell`, `compress_context`, `wait_time`
- `list_agent_runs`, `get_agent_run`, `spawn_agent`, `cancel_agent`, `steer_agent`
- `list_tool_runs`, `get_tool_run`, `wait_run`, `cancel_tool_run`
- `finish`

Role-specific availability is configurable via `[runtime.tools]`, including `steer_agent_scope` (`session` or `descendants`).

## Contract Principles

Agent-visible tool responses use a compact projection contract:

- no synthetic summary string is injected
- structured fields keep only decision-critical data
- low-value runtime noise is removed; input fields are returned only when they are decision-critical

Runtime persistence keeps full fidelity for replay/debugging:

- `tool_run.result` stores the raw internal result
- orchestrator projects raw results to the agent-visible contract before appending tool messages

## Per-Tool Contract

1. `wait_time`
- input: `seconds` (must be between `10` and `60`, inclusive)
- output success: `wait_time_status=true`
- output failure: `wait_time_status=false` with optional `timed_out`, `timeout_seconds`, `error`

2. `compress_context`
- input: no parameters
- output: `compressed`, `reason`, `summary_version`, `message_range`, `step_range`, `context_tokens_before`, `context_tokens_after`, `context_limit_tokens`
- output may include `error` when compression is disabled, misconfigured, or no-op
- execution scope is current agent only (no cross-agent compression)
- tool-call/control traces from `compress_context` are marked internal and excluded from later LLM requests
- timeout budget is configurable via `runtime.tool_timeouts.actions.compress_context` (default `180s`)

3. `list_agent_runs`
- input: `status`, `limit`, `cursor`
- output: `agent_runs_count`, `agent_runs`, `next_cursor`, `has_more`
- row fields: `id`, `name`, `role`, `status`, `created_at`, `summary_short`, `messages_count`
- status filter accepts `string|array` and is validated against agent status whitelist (`pending|running|paused|completed|failed|cancelled|terminated`)

4. `get_agent_run`
- input: `agent_id`, `messages_start`, `messages_end`
- message slice semantics: `[messages_start, messages_end)` where `messages_end` is exclusive
- `messages_start/messages_end` support negative indexes from the tail (e.g. `-1` means the last message)
- when slice params are omitted, defaults to the last 1 message
- max returned messages per call: 5 (messages can be long; avoid large fetches)
- invalid range inputs return explicit errors (out-of-range index, or normalized `end < start`)
- output: `agent_run` overview + `messages`
- when a requested slice is truncated by the 5-message cap, output also includes `warning` and `next_messages_start`
- each `messages` item is projected to a strict field subset: `content`, `reasoning`, `role`, `tool_calls`, `tool_call_id`
- `agent_run` fields: `id`, `name`, `role`, `status`, `created_at`, `parent_agent_id`, `children_count`, `step_count`

5. `spawn_agent`
- input: `name`, `instruction`
- output: `tool_run_id`, `child_agent_id`

6. `cancel_agent`
- input: `agent_id`, optional `recursive` (default `true`)
- output success: `cancel_agent_status=true`
- output failure: `cancel_agent_status=false` with optional `error`

7. `steer_agent`
- input: `agent_id`, `content`
- output success: `steer_agent_status=true`, `steer_run_id`, `target_agent_id`, `status`
- output failure: `steer_agent_status=false` with optional `configured_scope`, `error`
- runtime rejects self-steer; no steer run is created on rejected calls
- target reachability is governed by `[runtime.tools].steer_agent_scope`

8. `list_tool_runs`
- input: `status`, `limit`, `cursor`
- output: `tool_runs_count`, `tool_runs`, `next_cursor`, `has_more`
- status filter validation: `queued|running|completed|failed|cancelled`

9. `get_tool_run`
- input: `tool_run_id`, `include_result` (default `false`)
- output: `tool_run` overview
- when `include_result=true`, overview includes full `result`
- for `shell` runs, overview also includes `stdout`/`stderr`; when status is `running`, these come from accumulated stream output snapshots

10. `wait_run`
- input: exactly one of `tool_run_id` or `agent_id`
- output success: `wait_run_status=true`
- output failure: `wait_run_status=false` with optional `timed_out`, `timeout_seconds`, `error`
- agent wait success requires terminal status; `paused` is not a success state

11. `cancel_tool_run`
- input: `tool_run_id`
- output: `final_status`, `cancelled_agents_count`
- failure may include `error`
- cancel on terminal runs is a no-op; completed `spawn_agent` runs do not cancel child agents

12. `finish`
- input: `status`, `summary`, `next_recommendation` (worker only)
- output: `accepted` (failure may include `error`)
- root `finish.status` is restricted to `completed|partial`; worker keeps `completed|partial|failed`
- `follow_up_needed` is removed from tool input and not projected to tool messages
- `submitted_summary` is not projected to tool messages

## Pagination

List-style tools share cursor pagination policy:

- request fields: `limit` + `cursor`
- response fields: `next_cursor` + `has_more`
- default `limit`: `[runtime.tools].list_default_limit` (default 20)
- bounds: `1..[runtime.tools].list_max_limit` (default max 200)

Cursor encoding strategy:

- `list_agent_runs` uses opaque offset cursor
- `list_tool_runs` uses opaque `(created_at, id)` cursor for stable timeline ordering

## Tool Run Lifecycle

Every accepted tool action becomes a persisted `tool_run` with:

- identity: `toolrun-*`
- status: `queued` -> `running` -> `completed|failed|cancelled`
- timestamps: `created_at`, `started_at`, `completed_at`
- payload: arguments, raw result, error

## Execution Semantics

- most tools execute in blocking mode
- `shell` uses `[runtime.tools].shell_inline_wait_seconds` (default `5.0`): if the command does not finish in time, the tool returns `status=running`, `background=true`, `tool_run_id`, current `stdout`/`stderr`, and keeps running in the background
- `shell` supports local and remote (`direct` mode SSH) runtime paths under the same tool contract, selected by `[sandbox].backend` (`anthropic` or `none`)
- `spawn_agent` returns immediately after child creation (`child_agent_id` + `tool_run_id`)
- `steer_agent` reuses the same persisted steer-run pipeline as user/UI steer submission
- tool schemas do not expose per-call blocking overrides

### Remote Shell Path (SSH, V1)

- transport backend depends on `[sandbox].backend`:
  - `anthropic`: SSH + remote `srt --settings ...` execution
  - `none`: SSH + remote `/bin/bash --noprofile --norc -c ...` execution (unconstrained)
- runtime reuses session-level SSH ControlMaster sockets in both backends; remote settings file content-hash reuse applies to `anthropic`
- host key policy supports `accept_new` (default) and `strict`
- dependency policy is fail-closed for `anthropic`:
  - first-run dependency setup uses an extended timeout budget (`600s`) to tolerate package install latency
  - for missing `rg`, runtime attempts privileged auto-install via system package managers (`apt/dnf/yum/zypper/apk/pacman`) using root or `sudo -n`
  - for missing `bwrap`/`socat`, runtime attempts privileged auto-install of `bubblewrap`/`socat`
  - on apt-based systems, runtime enforces non-interactive apt env and attempts `dpkg --configure -a` + `apt-get -f install` repair before install retries
  - runtime requires `Node.js >= 18`; when missing/too old, runtime first attempts system package install of `nodejs`
  - on apt-based systems with CN locale/timezone hints, runtime first tries temporary TUNA mirror sources for `nodejs`, then falls back to default apt sources
  - if installed `nodejs` remains `<18`, runtime attempts NodeSource apt repo install (`node_20.x`) on supported apt distros (Debian/Ubuntu)
  - if NodeSource is unavailable or still `<18`, runtime falls back to user-space Node.js tarball install under `$HOME/.local/node-v20` (CN prefers TUNA `nodejs-release`, then `nodejs.org`)
  - for missing `srt` + missing `npm`, runtime attempts privileged `npm` install first, then installs `srt` in npm user-space path (`$HOME/.local`)
  - after setup, runtime executes `srt --help` as a startup smoke check to fail early on Node/runtime mismatch
  - any install failure still hard-fails the run/validation with explicit dependency errors
  - runtime runs a bubblewrap namespace capability preflight; if namespace creation is blocked (`Operation not permitted` / `kernel.unprivileged_userns_clone=0`), run/validation fails early with explicit guidance
  - setup status is emitted through shell stream lines prefixed with `[opencompany][remote-setup]`
- password auth uses one-time local temp files for `sshpass`; files are deleted after each command
- `none` backend does not enforce sandbox filesystem/network policies during shell execution (`network_policy`/`allowed_domains` are ignored at runtime)

## Validation and Metrics

- `validate_finish_action(...)` enforces role-field compatibility pre-execution
- `validate_wait_time_action(...)` and `validate_wait_run_action(...)` enforce wait tool constraints
- `tool_run_metrics(...)` computes totals, status counts, failure/cancel ratios, duration quantiles/histogram, and per-tool/per-agent aggregates
