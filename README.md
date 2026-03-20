# OpenCompany

Language: **English** | [中文](README_cn.md)

> ✨ In OpenCompany, every agent and user can self-organize to recruit or terminate teams, act autonomously, and collaborate through direct communication with other agents.
> 
> 🤖 Most of the code is completed by [Codex](https://openai.com/codex/).
> 
> 📮 Feedback: bebetterest@outlook.com

## 📚 Table of contents

- ✨ Features
- 📸 Screenshots
- 🚀 Quick start (Web UI first)
- 🔌 MCP usage guide
- 🖥️ TUI
- 💻 CLI command cheat sheet
- ⚙️ Configuration and debugging
- 🔒 Runtime safety model
- 🌐 Remote Direct Mode (SSH)
- 🗂️ Logs and persistence
- 🧱 Repository layout
- 📖 Further reading

## ✨ Features

- 🧩 Agent self-organization: each `agent` can create or terminate child teams, decompose and assign work, run feedback loops, message other agents, and submit results upstream.
- ⏱️ Asynchronous execution: agents can launch long-running tools asynchronously (for example subagent creation/delegation, inter-agent messaging, and long shell runs), inspect agent/tool state, and decide whether to keep going or block on waits.
- 🎛️ Steerability: users can create or terminate agents at any time, send steer messages to any agent, or open a terminal aligned with the same agent execution environment.
- 🧠 Context management: agents can autonomously call context compression tools on demand to control context growth while preserving key information.
- 📏 Limit policies: built-in limits cover tool-call duration, total created agents, and active agents; periodic reminder context is injected when step/context thresholds are reached, and overlong context is force-compressed.
- 🌐 Project environments: supports both local directories and remote SSH Linux directories as execution environments.
- 📝 Workspace modes: supports `Direct` and `Staged`; `Direct` writes to the project immediately, while `Staged` holds diffs for user approval before apply (remote supports `Direct` only).
- 🧰 Skills: sessions can enable reusable skill bundles from project/global sources; selected skills are materialized into `.opencompany_skills/<session_id>/...` and inherited by workers.
- 🔌 MCP client: sessions can enable configured MCP servers, expose discovered MCP tools directly to agents, browse/read MCP resources, and selectively expose workspace roots when safe.
- 🔒 Security model: supports both `anthropic` [sandbox (SRT)](https://github.com/anthropic-experimental/sandbox-runtime) and `none` (unconstrained) runtime backends.
- 🖥️ Three interfaces: supports Web UI / TUI / CLI, with Web UI recommended (bilingual visualization in Chinese/English covers session overview, collaboration graph, per-agent details, tool/steer traces, and operations like session create/import, config updates, agent create/steer/terminate, and opening agent terminals).
- 🤖 LLM access: supports model calls through [OpenRouter](https://openrouter.ai/).

## 📸 Screenshots

![OpenCompany Web UI Overview](screenshots/screenshot_en1.png)

![OpenCompany Web UI Agent Details](screenshots/screenshot_en2.png)

## 🚀 Quick start (Web UI first)

1. Set up a Python environment (choose one path; both include editable install and dev deps):

```bash
# Option A: Conda
conda env create -f environment.yml
conda activate OpenCompany

# Option B: uv
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

`uv` installs Python packages only. Install the required system tools before continuing:

```bash
# macOS local machine (Homebrew)
brew install ripgrep node

# Debian/Ubuntu Linux host (includes the full Anthropic sandbox dependency set)
sudo apt-get update
sudo apt-get install -y ripgrep bubblewrap socat nodejs npm
```

`npm` is usually bundled with Node.js. For the default Anthropic sandbox backend, the Linux execution host also needs `bubblewrap` (`bwrap`) and `socat`; verify `node --version` reports `18` or newer before continuing.

2. Configure your OpenRouter API key:

```bash
export OPENROUTER_API_KEY="your_api_key"
```

Optional:

```bash
cp .env.example .env
```

3. If you use `[sandbox].backend = "anthropic"` (default), install Node dependency for sandbox runtime:

```bash
npm install
```

4. If you use remote password auth (`--remote-auth password`), install local `sshpass`:

```bash
# macOS
brew install hudochenkov/sshpass/sshpass

# Debian/Ubuntu
sudo apt-get install sshpass
```

5. Launch Web UI:

```bash
opencompany ui
```

Default address: `http://127.0.0.1:8765`

```bash
opencompany ui --host 0.0.0.0 --port 9090
```

## 🔌 MCP usage guide

This repository now preconfigures four MCP servers in `opencompany.toml`: two enabled official hosted servers (`huggingface`, `notion`) plus two optional presets (`github`, `duckduckgo`).

1. Define MCP servers in `opencompany.toml`:

```toml
[mcp]
protocol_version = "2025-11-25"

[mcp.servers.huggingface]
transport = "streamable_http"
enabled = true
title = "Hugging Face MCP"
timeout_seconds = 30
url = "https://huggingface.co/mcp?login"
oauth_enabled = true

[mcp.servers.notion]
transport = "streamable_http"
enabled = true
title = "Notion MCP"
timeout_seconds = 30
url = "https://mcp.notion.com/mcp"
oauth_enabled = true
oauth_authorization_prompt = "consent"
oauth_use_resource_param = false

[mcp.servers.github]
transport = "streamable_http"
enabled = false
title = "GitHub MCP"
timeout_seconds = 30
url = "https://api.githubcopilot.com/mcp/"
headers = { Authorization = "env:GITHUB_MCP_AUTHORIZATION" }

[mcp.servers.duckduckgo]
transport = "stdio"
enabled = false
title = "DuckDuckGo MCP"
timeout_seconds = 45
command = "duckduckgo-mcp-server"
args = []
env = { DDG_SAFE_SEARCH = "MODERATE", DDG_REGION = "wt-wt" }
```

2. Prepare environment variables and local MCP dependencies:

```bash
# GitHub MCP auth header (must include "Bearer " prefix)
export GITHUB_MCP_AUTHORIZATION="Bearer <your_github_pat>"

# Or persist in .env
echo 'GITHUB_MCP_AUTHORIZATION=Bearer <your_github_pat>' >> .env

# DuckDuckGo MCP dependency (community server)
# Option A: inside OpenCompany Conda environment
conda run -n OpenCompany python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"

# Option B: in your active Python environment
python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"
```

3. Validate the config from CLI before opening the UI:

```bash
opencompany mcp-servers
opencompany mcp-servers --mcp-server filesystem
```

4. For OAuth-enabled hosted servers such as Hugging Face and Notion, complete login once before using them:

```bash
opencompany mcp-login --mcp-server huggingface
opencompany mcp-login --mcp-server notion
```

5. In Web UI:

- The `Skills` and `MCP Servers` sections start collapsed by default; expand the section header to manage selections.
- Configured MCP servers are preloaded from `opencompany.toml` on page load; `Discover` refreshes the catalog.
- Click tiles directly to enable or disable skills and MCP servers; manual text input is no longer part of the Web UI flow.
- OAuth-enabled MCP cards expose `Login` / `Continue Login` / `Re-login` plus `Clear Auth`; while a login is pending the button stays clickable so the authorization page can be reopened if it was closed accidentally, and `Clear Auth` drops the stored OAuth record so a hosted server can be fully reconnected from scratch.
- Click `Use Defaults` to mirror servers with `enabled = true`, or use `Select All` to override the config for the current run.
- Start or continue a session after selection; the MCP panel will then show connection status, roots exposure, tool/resource counts, protocol version, and warnings for each server.

6. CLI run/resume equivalents:

```bash
opencompany run --mcp-server huggingface --mcp-server notion "Inspect this repository and propose next engineering steps."
opencompany run --mcp-server github --mcp-server duckduckgo "Research dependency risks and summarize latest references."
opencompany resume <session_id> --mcp-server github "new instruction"
```

Notes:

- Web UI selections are per-run overrides; they do not rewrite `opencompany.toml`.
- Configured MCP servers are loaded at Web UI bootstrap; `Discover` refreshes the configured catalog, while live connection/tool/resource status appears only after a session actually materializes MCP for an agent.
- `opencompany mcp-login --mcp-server <id>` performs MCP OAuth discovery, PKCE-based authorization, dynamic client registration when available, and local token persistence for later runs.
- OpenCompany now serializes OAuth refresh per server inside the runtime process, so concurrent agents do not race the same refresh token after a hosted MCP returns `401`.
- When an OAuth-enabled hosted MCP keeps returning `401` during MCP connection setup (for example `invalid_token`, expired login, or missing refresh token), OpenCompany now clears that server's local OAuth record so the next login starts from a clean state.
- GitHub's hosted MCP endpoint is `https://api.githubcopilot.com/mcp/`. The preset uses an `Authorization` header sourced from `GITHUB_MCP_AUTHORIZATION` (set it to a full value such as `Bearer <token>`). Keep this as `headers = { Authorization = "env:GITHUB_MCP_AUTHORIZATION" }` so the Web UI MCP card can show the `Login Config` action.
- DuckDuckGo MCP preset uses the community `duckduckgo-mcp-server` executable. Install it first in your active environment (for example: `python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"`). Under the default constrained sandbox, search/fetch results are still bounded by your network policy.
- Hugging Face works as a hosted Streamable HTTP MCP server at `https://huggingface.co/mcp`; Hugging Face also documents OAuth login via `https://huggingface.co/mcp?login`. OpenCompany accepts `?login` in config, but strips that query flag before runtime MCP transport requests.
- Notion provides an official hosted MCP server at `https://mcp.notion.com/mcp`, and its official integration guide uses OAuth 2.0 Authorization Code + PKCE + token refresh, plus a direct `Authorization: Bearer` MCP connection. OpenCompany keeps `oauth_authorization_prompt = "consent"` and enables the OAuth `resource` parameter so token exchange/refresh can target the protected resource metadata exposed by Notion.
- For hosted servers configured on a `.../mcp` URL, OpenCompany now retries the sibling `.../sse` endpoint automatically when initial Streamable HTTP MCP initialization fails. This matches Notion's recommendation to try Streamable HTTP first and fall back to SSE if needed.

## 🖥️ TUI

TUI is still supported as a fallback interface:

```bash
opencompany tui
opencompany tui --project-dir /path/to/target
opencompany tui --workspace-mode staged
opencompany tui --session-id <session_id>
opencompany tui --remote-target demo@example.com --remote-dir /home/demo/workspace --remote-auth key --remote-key-path ~/.ssh/id_ed25519
```

Rules:

- Remote flags are for creating new sessions only.
- Do not combine remote flags with `--session-id`.
- Do not combine `--workspace-mode` with `--session-id`.
- `staged` mode does not support remote workspace.
- Loading an existing session with `--session-id` binds the original session directly; it does not create an implicit clone.

## 💻 CLI command cheat sheet

Run one task in current directory:

```bash
opencompany run "Inspect this repository and propose next engineering steps."
```

Run against another project:

```bash
opencompany run --project-dir /path/to/target "Inspect this repository and propose next engineering steps."
```

Run in staged mode:

```bash
opencompany run --workspace-mode staged "Inspect this repository and propose next engineering steps."
```

Run with an explicit sandbox backend, model, and root agent name:

```bash
opencompany run \
  --sandbox-backend none \
  --model openai/gpt-4.1-mini \
  --root-agent-name "Planner Root" \
  "Inspect this repository and propose next engineering steps."
```

Discover available skills:

```bash
opencompany skills
opencompany skills --project-dir /path/to/target
opencompany skills --remote-target demo@example.com:22 --remote-dir /home/demo/workspace --remote-auth key --remote-key-path ~/.ssh/id_ed25519
```

Add a skill:

- Put the skill directory under either `<project_dir>/skills/` or `<app_dir>/skills/`.
- It is not enough to create only the folder name; a valid skill must include at least `skill.toml` and `SKILL.md`.
- If the same `skill_id` exists in both places, the project source overrides the global source.
- This repository already includes a default bundled set of skills under `skills/` (including `hf-cli`, adapted from Hugging Face Skills).

```text
<project_dir>/skills/<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # optional
  resources/...      # optional; may contain text, scripts, or binary files
```

Minimal `skill.toml` example:

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

Verify discovery after adding it:

```bash
opencompany skills --project-dir /path/to/target
```

Example import from Hugging Face Skills:

```bash
python3 skills/skill-installer/resources/scripts/install-skill-from-github.py \
  --repo huggingface/skills \
  --path skills/hf-cli \
  --dest skills
```

Run with explicit skills:

```bash
opencompany run \
  --skill repo-map \
  --skill release-notes \
  "Inspect this repository and propose next engineering steps."
```

Inspect configured MCP servers:

```bash
opencompany mcp-servers
opencompany mcp-servers --mcp-server filesystem
```

Run with explicit MCP servers:

```bash
opencompany run \
  --mcp-server filesystem \
  --mcp-server docs \
  "Inspect this repository and propose next engineering steps."
```

Run in direct mode against remote SSH workspace:

```bash
opencompany run \
  --remote-target demo@example.com:22 \
  --remote-dir /home/demo/workspace \
  --remote-auth key \
  --remote-key-path ~/.ssh/id_ed25519 \
  --remote-known-hosts accept_new \
  "Inspect this repository and propose next engineering steps."
```

Continue an existing session:

```bash
opencompany resume <session_id> "new instruction"
opencompany resume <session_id> --sandbox-backend anthropic --model openai/gpt-4.1-mini "new instruction"
opencompany resume <session_id> --skill repo-map --skill release-notes "new instruction"
```

Clone an existing session first when you want a branch copy:

```bash
opencompany clone <session_id>
opencompany clone <session_id> --app-dir /path/to/app
```

Apply / undo staged project sync:

```bash
opencompany apply <session_id>
opencompany undo <session_id>
# non-interactive
opencompany apply <session_id> --yes
opencompany undo <session_id> --yes
```

Export session logs:

```bash
opencompany export-logs <session_id>
opencompany export-logs <session_id> --export-path /tmp/session-export.json
```

Query persisted messages:

```bash
opencompany messages <session_id>
opencompany messages <session_id> --agent-id <agent_id> --tail 100
opencompany messages <session_id> --cursor <next_cursor> --include-extra --format text
```

Query persisted tool runs:

```bash
opencompany tool-runs <session_id>
opencompany tool-runs <session_id> --status running --limit 200 --cursor <next_cursor>
```

Tool-run metrics:

```bash
opencompany tool-run-metrics <session_id>
opencompany tool-run-metrics <session_id> --export
opencompany tool-run-metrics <session_id> --export --export-path /tmp/tool_run_metrics.json
```

Open session terminal or run terminal parity self-check:

```bash
opencompany terminal <session_id>
opencompany terminal <session_id> --self-check
```

Notes:

- `opencompany run` and `opencompany resume` show a live status panel in interactive terminals.
- `opencompany resume <session_id> ...` continues the original session in place; use `opencompany clone <session_id>` first if you want a forked copy.
- Panel supports auto pagination every `5s`; press `=` / `+` / `-` for manual page switching.
- Use `--preview-chars N` to adjust per-field preview width (default `256`).
- Use `--sandbox-backend <name>` on `run` / `resume` to override `[sandbox].backend` for that invocation only.
- `opencompany run` also accepts `--model <model>` and `--root-agent-name <name>`; `opencompany resume` also accepts `--model <model>`.

## ⚙️ Configuration and debugging

`opencompany.toml` is the source of truth.

Key config groups:

- `[project]`: app name, default locale, runtime data dir.
- `[llm.openrouter]`: model(s), retry policy, timeout, sampling.
- `[runtime.limits]`: orchestration limits (children/active agents/step budgets/reminder intervals).
- `[runtime.tool_timeouts]`: default and per-tool timeouts.
- `[runtime.tools]`: root/worker tool allowlists, `steer_agent_scope`, list pagination, shell inline wait.
- `[runtime.context]`: context pressure detection and compression settings.
- `[sandbox]`: backend, network policy, allowlist domains, sandbox timeout.
- `[logging]`: session event/export/diagnostics filenames.
- `[locale]`: fallback locale when system locale cannot be resolved.

Current defaults in this repository include:

- `[project].default_locale = "zh"` (set `auto` to follow system locale)
- `[llm.openrouter].model = "stepfun/step-3.5-flash:free"`
- `[llm.openrouter].max_retries = 8`
- `[runtime.tool_timeouts].default_seconds = 30`
- `[runtime.tool_timeouts].shell_seconds = 300`
- `[runtime.tools].shell_inline_wait_seconds = 10`
- `[runtime.context].max_context_tokens = 51200`
- `[sandbox].backend = "anthropic"`
- `[sandbox].network_policy = "allowlist"`
- `[sandbox].timeout_seconds = 300`
- `[locale].fallback = "en"`

Debugging:

```bash
opencompany run --debug "Inspect this repository and propose next engineering steps."
opencompany resume <session_id> "new instruction" --debug
opencompany ui --debug
opencompany tui --debug
```

With `--debug`, API request/response traces and per-stage timing traces are written to `.opencompany/sessions/<session_id>/debug/`.

## 🔒 Runtime safety model

- Root and worker share one tool protocol; prompts keep root orchestration-first.
- Agent execution stays isolated in per-agent workspaces under explicit budgets.
- Worker changes are promoted to parent workspace only after worker completion.
- In `staged` mode, root `finish` only stages changes; explicit `opencompany apply <session_id>` is required.
- Applied sync can be reverted with `opencompany undo <session_id>`.
- Runtime enforces explicit session/agent lifecycle states:
  - session: `running|completed|interrupted|failed`
  - completion quality: `completed|partial` (only when session is `completed`)
  - agent: `pending|running|paused|completed|failed|cancelled|terminated`

## 🌐 Remote Direct Mode (SSH)

Scope and constraints:

- Supported only in `direct` workspace mode.
- Remote host must be Linux.
- Session stores remote config at `.opencompany/sessions/<session_id>/remote_session.json`.
- Password is never stored in session config.

Auth and local requirements:

- `--remote-auth key`: requires `--remote-key-path`.
- `--remote-auth password`: prompts at runtime; local `sshpass` is required.
- `--remote-known-hosts` supports `accept_new` (default) and `strict`.

Backend behavior:

- `anthropic`: fail-closed remote sandbox path using `srt` with enforced policy.
- `none`: direct remote `/bin/bash` execution without sandbox file/network restrictions.

Dependency setup (anthropic backend):

- Runtime performs fail-closed dependency checks/setup on remote host.
- It validates or attempts setup for essentials like `rg`, `bwrap`, `socat`, `Node.js >= 18`, and `srt`.
- If requirements cannot be satisfied, run/validation fails with explicit error details.

## 🗂️ Logs and persistence

- Session events: `.opencompany/sessions/<session_id>/events.jsonl`
- Per-agent messages: `.opencompany/sessions/<session_id>/<agent_id>_messages.jsonl`
- Optional API debug traces: `.opencompany/sessions/<session_id>/debug/<agent_id>__<module>.jsonl`
- Optional debug timing traces: `.opencompany/sessions/<session_id>/debug/timings.jsonl`
- Remote session config (when remote): `.opencompany/sessions/<session_id>/remote_session.json`
- Cross-session diagnostics: `.opencompany/diagnostics.jsonl`

`events.jsonl` / `export.json` / `diagnostics.jsonl` filenames are configurable in `[logging]`.

## 🧱 Repository layout

- `src/opencompany/`: core Python package
- `src/opencompany/webui/`: Web UI backend + static frontend
- `src/opencompany/tui/`: Textual TUI
- `src/opencompany/tools/`: tool schema registry and executors
- `src/opencompany/orchestration/`: agent runtime and orchestration flow
- `prompts/`: English prompts and Chinese mirrors
- `docs/`: docs index, architecture, technical route, message references (+ Chinese mirrors)
- `docs/modules/`: subsystem references
- `tests/`: unit/integration-oriented tests

## 📖 Further reading

- `docs/README.md`
- `docs/technical_route.md`
- `docs/architecture.md`
- `docs/message_flow.md`
- `docs/message_stream_map.md`
- `docs/modules/runtime_core.md`
- `docs/modules/tool_runtime.md`
- `docs/modules/ui_surfaces.md`
- Chinese mirrors: `README_cn.md`, `docs/*_cn.md`
