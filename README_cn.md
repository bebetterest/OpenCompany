# OpenCompany

语言： [English](README.md) | **中文**

> ✨ 在 OpenCompany，每个 agent 与用户都能自组织地招募或终止团队，自主行动，并与其他 agent 协同沟通。
> 
> 🤖 代码基本由 [Codex](https://openai.com/codex/) 完成。
> 
> 📮 反馈：bebetterest@outlook.com

## 📚 目录

- ✨ 功能
- 📸 界面截图
- 🚀 快速上手（推荐 Web UI）
- 🔌 MCP 使用指南
- 🖥️ TUI
- 💻 CLI 常用命令
- ⚙️ 配置与调试
- 🔒 运行时安全模型
- 🌐 Remote Direct 模式（SSH）
- 🗂️ 日志与持久化
- 🧱 仓库结构
- 📖 延伸阅读

## ✨ 功能

- 🧩 Agent 自组织：每个 `agent` 都可创建或终止子团队，拆解并分配任务、形成反馈循环；可向其他 agent 发送消息，完成后向上级提交结果。
- ⏱️ 异步执行：agent 可异步触发长耗时工具（如创建并分派子 agent、跨 agent 消息、长时 shell），并可自主查询 agent / tool 状态，决策继续执行或阻塞等待。
- 🎛️ Steerability：用户可随时创建、终止 agent，向任意 agent 发送 steer 消息，或打开与 agent 环境一致的终端直接操作。
- 🧠 上下文管理：agent 可自主按需调用上下文压缩工具，控制上下文膨胀并保留关键信息。
- 📏 限制策略：内置工具调用时长限制、可创建 agent 数限制、活跃 agent 数限制；当步数或上下文达到阈值时定期注入提醒；上下文超长会强制压缩。
- 🌐 项目环境：支持本地目录与远程 SSH Linux 目录作为执行环境。
- 📝 工作区模式：支持 `Direct` 与 `Staged`；`Direct` 直接写入项目目录，`Staged` 先暂存 diff 并在用户审批后应用（远程仅支持 `Direct`）。
- 🧰 Skills：session 可启用来自项目源/全局源的可复用 skill bundle；选中的 skills 会物化到 `.opencompany_skills/<session_id>/...`，并由 workers 继承使用。
- 🔌 MCP client：session 可启用已配置的 MCP servers，把发现到的 MCP tools 直接暴露给 agents，并支持 MCP resources 浏览/读取，以及在安全前提下暴露 workspace roots。
- 🔒 安全性：支持 `anthropic` [sandbox（SRT）](https://github.com/anthropic-experimental/sandbox-runtime) 与 `none`（不受限）两种运行后端。
- 🖥️ 三种界面：支持 Web UI / TUI / CLI，推荐 Web UI（可视化支持中英双语，可查看会话总览、协作结构、各 agent 详情、工具与引导信息，并支持创建/导入会话、修改配置、创建/引导/终止 agent、打开 agent 环境终端等操作）。
- 🤖 LLM 接入：支持通过 [OpenRouter](https://openrouter.ai/) 调用模型。

## 📸 界面截图

![OpenCompany Web UI 总览](screenshots/screenshot_cn1.png)

![OpenCompany Web UI Agent 详情](screenshots/screenshot_cn2.png)

## 🚀 快速上手（推荐 Web UI）

1. 准备 Python 环境（两种方式任选其一，均包含可编辑安装与开发依赖）：

```bash
# 方案 A：Conda
conda env create -f environment.yml
conda activate OpenCompany

# 方案 B：uv
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

`uv` 只负责安装 Python 包。继续之前，请先安装所需的系统工具：

```bash
# macOS 本机（Homebrew）
brew install ripgrep node

# Debian/Ubuntu Linux 主机（包含 Anthropic sandbox 的完整依赖集）
sudo apt-get update
sudo apt-get install -y ripgrep bubblewrap socat nodejs npm
```

`npm` 通常会随 Node.js 一起安装。若使用默认的 Anthropic sandbox 后端，Linux 执行主机还需要安装 `bubblewrap`（`bwrap`）和 `socat`；继续前请先确认 `node --version` 输出为 `18` 或更高版本。

2. 配置 OpenRouter API Key：

```bash
export OPENROUTER_API_KEY="your_api_key"
```

可选：

```bash
cp .env.example .env
```

3. 若使用 `[sandbox].backend = "anthropic"`（默认），安装 sandbox runtime 的 Node 依赖：

```bash
npm install
```

4. 若使用远程密码认证（`--remote-auth password`），在本机安装 `sshpass`：

```bash
# macOS
brew install hudochenkov/sshpass/sshpass

# Debian/Ubuntu
sudo apt-get install sshpass
```

5. 启动 Web UI：

```bash
opencompany ui
```

默认地址：`http://127.0.0.1:8765`

```bash
opencompany ui --host 0.0.0.0 --port 9090
```

6. 开发前先验证本地环境：

```bash
pytest -q
```

请在已激活的 `OpenCompany` Conda 环境或当前 `uv` 虚拟环境中执行测试，避免出现导入路径或可选依赖不一致问题。

## 🔌 MCP 使用指南

当前仓库已在 `opencompany.toml` 预置四个 MCP，且四个默认都启用（`huggingface`、`notion`、`github`、`duckduckgo`）。

1. 先在 `opencompany.toml` 里定义 MCP servers：

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
enabled = true
title = "GitHub MCP"
timeout_seconds = 30
url = "https://api.githubcopilot.com/mcp/"
headers = { Authorization = "env:GITHUB_MCP_AUTHORIZATION" }

[mcp.servers.duckduckgo]
transport = "stdio"
enabled = true
title = "DuckDuckGo MCP"
timeout_seconds = 45
command = "duckduckgo-mcp-server"
args = []
env = { DDG_SAFE_SEARCH = "MODERATE", DDG_REGION = "wt-wt" }
```

2. 准备环境变量与本地 MCP 依赖：

```bash
# GitHub MCP 鉴权头（必须包含 "Bearer " 前缀）
export GITHUB_MCP_AUTHORIZATION="Bearer <your_github_pat>"

# 或持久化写入 .env
echo 'GITHUB_MCP_AUTHORIZATION=Bearer <your_github_pat>' >> .env

# DuckDuckGo MCP 依赖（社区 server）
# 方案 A：在 OpenCompany Conda 环境中安装
conda run -n OpenCompany python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"

# 方案 B：在当前激活的 Python 环境安装
python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"
```

3. 打开 UI 前，可先用 CLI 校验配置：

```bash
opencompany mcp-servers
opencompany mcp-servers --mcp-server filesystem
```

4. 对 Hugging Face、Notion 这类启用了 OAuth 的 hosted MCP，先登录一次：

```bash
opencompany mcp-login --mcp-server huggingface
opencompany mcp-login --mcp-server notion
```

5. Web UI 中的使用方式：

- `Skills` 和 `MCP Servers` 两块默认折叠；先展开对应标题栏，再进行选择和操作。
- 页面加载时就会从 `opencompany.toml` 预加载已配置的 MCP servers；`发现` 用于刷新目录。
- skills 和 MCP servers 都改成了直接点卡片启用/停用，不再需要手动输入。
- 两个选择器现在统一为分层布局（`概览` → `已选` → `目录` → `告警`），折叠头摘要只保留关键计数，避免重复状态文案。
- skill/MCP 卡片默认展示精简信息，可按卡片展开详情查看高级元数据。
- 对启用了 OAuth 的 MCP 卡片，界面会直接显示 `登录` / `继续登录` / `重新登录` 和 `清除登录`；当登录仍在处理中时按钮不会被锁死，误关授权页后可直接重新打开，而 `清除登录` 会删除本地保存的 OAuth 记录，便于彻底重新授权。
- 点击 `用默认项` 可同步 `enabled = true` 的默认集合；也可以用 `全选` 覆盖本次运行的配置默认项。
- 完成选择后再启动或继续 session；此时 MCP 面板会显示每个 server 的连接状态、roots 暴露情况、tool/resource 数量、协议版本与告警信息。

6. CLI 中对应的 run/resume 写法：

```bash
opencompany run --mcp-server huggingface --mcp-server notion "Inspect this repository and propose next engineering steps."
opencompany run --mcp-server github --mcp-server duckduckgo "Research dependency risks and summarize latest references."
opencompany resume <session_id> --mcp-server github "new instruction"
```

补充说明：

- Web UI 中的选择只覆盖当前这次运行，不会回写 `opencompany.toml`。
- Web UI 启动时就会加载已配置的 MCP servers；`发现` 负责刷新配置目录。真正的连接状态以及 tool/resource 运行态，要等 session 为 agent 完成 MCP 物化后才会出现。
- `opencompany mcp-login --mcp-server <id>` 会执行 MCP OAuth discovery、PKCE 授权、可用时的动态 client 注册，并把 token 持久化给后续运行复用。
- OpenCompany 现在会在同一运行进程内按 server 串行化 OAuth refresh，避免多个并发 agent 在 hosted MCP 返回 `401` 后抢用同一份 refresh token。
- 当某个启用 OAuth 的 hosted MCP 在连接初始化阶段持续返回 `401`（例如 `invalid_token`、登录过期或缺少 refresh token）时，OpenCompany 会自动清理该 server 的本地 OAuth 记录，确保下一次登录从干净状态开始。
- GitHub 的 hosted MCP 地址是 `https://api.githubcopilot.com/mcp/`。预置配置通过环境变量 `GITHUB_MCP_AUTHORIZATION` 提供 `Authorization` 头（值需为完整头值，例如 `Bearer <token>`）。请保持 `headers = { Authorization = "env:GITHUB_MCP_AUTHORIZATION" }`，这样 Web UI 的 MCP 卡片才会显示“登录配置”动作。
- DuckDuckGo MCP 预置使用社区 `duckduckgo-mcp-server` 可执行程序。请先在当前运行环境安装（例如：`python -m pip install "duckduckgo-mcp-server @ git+https://github.com/nickclyde/duckduckgo-mcp-server.git"`）。在默认受约束 sandbox 下，其搜索/抓取能力仍受网络策略限制。
- Hugging Face 官方提供 hosted Streamable HTTP MCP，基础地址是 `https://huggingface.co/mcp`；官方也提供 `https://huggingface.co/mcp?login` 作为 OAuth 登录入口。OpenCompany 支持在配置中使用 `?login`，但在运行态发起 MCP 传输请求时会自动去掉该查询标记。
- Notion 官方提供 hosted MCP，地址是 `https://mcp.notion.com/mcp`；其官方接入文档采用 OAuth 2.0 Authorization Code + PKCE + token refresh，并通过 `Authorization: Bearer` 连接 MCP。OpenCompany 仍保持 `oauth_authorization_prompt = "consent"`，并启用 OAuth `resource` 参数，让 token 交换/刷新与 Notion 暴露的受保护资源元数据保持一致。
- 对于配置成 `.../mcp` 的 hosted server，若初次 Streamable HTTP MCP 初始化失败，OpenCompany 现在会自动重试同级的 `.../sse` 端点。这与 Notion 官方“先试 Streamable HTTP，不行再回退 SSE”的建议一致。

## 🖥️ TUI

TUI 仍然可用，可作为回退界面：

```bash
opencompany tui
opencompany tui --project-dir /path/to/target
opencompany tui --workspace-mode staged
opencompany tui --session-id <session_id>
opencompany tui --remote-target demo@example.com --remote-dir /home/demo/workspace --remote-auth key --remote-key-path ~/.ssh/id_ed25519
```

规则：

- 远程参数仅用于新建会话。
- 不要将远程参数与 `--session-id` 同时使用。
- 不要将 `--workspace-mode` 与 `--session-id` 同时使用。
- `staged` 模式不支持远程工作区。
- 使用 `--session-id` 加载已有会话时，会直接绑定原 session，不会再隐式 clone。

## 💻 CLI 常用命令

在当前目录执行任务：

```bash
opencompany run "Inspect this repository and propose next engineering steps."
```

对其他项目目录执行任务：

```bash
opencompany run --project-dir /path/to/target "Inspect this repository and propose next engineering steps."
```

使用 staged 模式：

```bash
opencompany run --workspace-mode staged "Inspect this repository and propose next engineering steps."
```

显式指定 sandbox backend、模型和 root agent 名称：

```bash
opencompany run \
  --sandbox-backend none \
  --model openai/gpt-4.1-mini \
  --root-agent-name "Planner Root" \
  "Inspect this repository and propose next engineering steps."
```

发现可用 skills：

```bash
opencompany skills
opencompany skills --project-dir /path/to/target
opencompany skills --remote-target demo@example.com:22 --remote-dir /home/demo/workspace --remote-auth key --remote-key-path ~/.ssh/id_ed25519
```

添加 skill：

- 把 skill 目录放到 `<project_dir>/skills/` 或 `<app_dir>/skills/` 下即可参与发现。
- 但不是只有目录名就行；一个有效 skill 至少要包含 `skill.toml` 和 `SKILL.md`。
- 如果项目源和全局源里存在同一个 `skill_id`，项目源会覆盖全局源。
- 本仓库已经在 `skills/` 下内置一组默认 skills，全部由 Codex Skills 迁移并适配为 OpenCompany skill 格式。

```text
<project_dir>/skills/<skill_id>/
  skill.toml
  SKILL.md
  SKILL_cn.md        # 可选
  resources/...      # 可选；可包含文本、脚本或二进制文件
```

最小 `skill.toml` 示例：

```toml
[skill]
id = "repo-map"
name = "Repo Map"
name_cn = "仓库地图"
description = "Explain the repository layout and key entry points."
description_cn = "解释仓库结构和关键入口。"
tags = ["docs", "navigation"]
```

添加后可用下面的命令确认是否发现成功：

```bash
opencompany skills --project-dir /path/to/target
```

从 Hugging Face Skills 导入示例：

```bash
python3 skills/skill-installer/resources/scripts/install-skill-from-github.py \
  --repo huggingface/skills \
  --path skills/hf-cli \
  --dest skills
```

显式启用 skills 运行：

```bash
opencompany run \
  --skill repo-map \
  --skill release-notes \
  "Inspect this repository and propose next engineering steps."
```

检查当前已配置的 MCP servers：

```bash
opencompany mcp-servers
opencompany mcp-servers --mcp-server filesystem
```

显式启用 MCP servers 运行：

```bash
opencompany run \
  --mcp-server filesystem \
  --mcp-server docs \
  "Inspect this repository and propose next engineering steps."
```

在 direct 模式下连接远程 SSH 工作区执行：

```bash
opencompany run \
  --remote-target demo@example.com:22 \
  --remote-dir /home/demo/workspace \
  --remote-auth key \
  --remote-key-path ~/.ssh/id_ed25519 \
  --remote-known-hosts accept_new \
  "Inspect this repository and propose next engineering steps."
```

继续已有会话：

```bash
opencompany resume <session_id> "new instruction"
opencompany resume <session_id> --sandbox-backend anthropic --model openai/gpt-4.1-mini "new instruction"
opencompany resume <session_id> --skill repo-map --skill release-notes "new instruction"
```

若希望先分叉出一个副本，再继续执行：

```bash
opencompany clone <session_id>
opencompany clone <session_id> --app-dir /path/to/app
```

应用 / 撤销 staged 写回：

```bash
opencompany apply <session_id>
opencompany undo <session_id>
# 非交互
opencompany apply <session_id> --yes
opencompany undo <session_id> --yes
```

导出会话日志：

```bash
opencompany export-logs <session_id>
opencompany export-logs <session_id> --export-path /tmp/session-export.json
```

查询持久化消息：

```bash
opencompany messages <session_id>
opencompany messages <session_id> --agent-id <agent_id> --tail 100
opencompany messages <session_id> --cursor <next_cursor> --include-extra --format text
```

查询持久化 tool runs：

```bash
opencompany tool-runs <session_id>
opencompany tool-runs <session_id> --status running --limit 200 --cursor <next_cursor>
```

tool-run 指标：

```bash
opencompany tool-run-metrics <session_id>
opencompany tool-run-metrics <session_id> --export
opencompany tool-run-metrics <session_id> --export --export-path /tmp/tool_run_metrics.json
```

打开会话终端或执行终端策略一致性自检：

```bash
opencompany terminal <session_id>
opencompany terminal <session_id> --self-check
```

补充说明：

- `opencompany run` 与 `opencompany resume` 在交互终端会显示动态状态面板。
- `opencompany resume <session_id> ...` 会直接在原 session 上继续；若要保留原 session 并创建分支副本，请先执行 `opencompany clone <session_id>`。
- 面板默认每 `5s` 自动分页；可按 `=` / `+` / `-` 手动切页。
- 用 `--preview-chars N` 调整各字段预览宽度（默认 `256`）。
- 在 `run` / `resume` 中可用 `--sandbox-backend <name>` 仅覆盖本次调用的 `[sandbox].backend`。
- `opencompany run` 还支持 `--model <model>` 与 `--root-agent-name <name>`；`opencompany resume` 还支持 `--model <model>`。

## ⚙️ 配置与调试

`opencompany.toml` 是唯一配置事实来源。

核心配置分组：

- `[project]`：应用名、默认语言、运行数据目录。
- `[llm.openrouter]`：模型、重试策略、超时、采样参数。
- `[runtime.limits]`：编排限制（子 agent 数、活跃 agent 数、步数预算、提醒间隔）。
- `[runtime.tool_timeouts]`：默认与各工具超时。
- `[runtime.tools]`：root/worker 工具白名单、`steer_agent_scope`、列表分页、shell 前台等待时长。
- `[runtime.context]`：上下文压力检测与压缩配置。
- `[sandbox]`：后端、网络策略、域名白名单、超时。
- `[logging]`：会话事件/导出/诊断日志文件名。
- `[locale]`：系统语言无法识别时的回退语言。

当前仓库默认值包括：

- `[project].default_locale = "auto"`（默认跟随系统语言；若系统语言不是中英文则回退到英文）
- `[llm.openrouter].model = "qwen/qwen3.6-plus-preview:free"`
- `[llm.openrouter].max_retries = 8`
- `[runtime.tool_timeouts].default_seconds = 30`
- `[runtime.tool_timeouts].shell_seconds = 300`
- `[runtime.tools].shell_inline_wait_seconds = 10`
- `[runtime.context].max_context_tokens = 51200`
- `[sandbox].backend = "anthropic"`
- `[sandbox].network_policy = "allowlist"`
- `[sandbox].timeout_seconds = 300`
- `[locale].fallback = "en"`

调试方式：

```bash
opencompany run --debug "Inspect this repository and propose next engineering steps."
opencompany resume <session_id> "new instruction" --debug
opencompany ui --debug
opencompany tui --debug
```

启用 `--debug` 后，API 请求/响应追踪与分阶段耗时追踪会写入 `.opencompany/sessions/<session_id>/debug/`。

## 🔒 运行时安全模型

- root 与 worker 共享同一工具协议；prompt 会持续约束 root 以编排为先。
- agent 在隔离 workspace 中执行，并受显式预算约束。
- worker 完成后，改动才会提升回父 workspace。
- `staged` 模式下，root `finish` 只会暂存改动；需显式执行 `opencompany apply <session_id>` 才会写回项目。
- 已写回改动可用 `opencompany undo <session_id>` 回滚。
- 运行时使用明确状态机：
  - session：`running|completed|interrupted|failed`
  - completion 质量：`completed|partial`（仅在 session `completed` 时写入）
  - agent：`pending|running|paused|completed|failed|cancelled|terminated`

## 🌐 Remote Direct 模式（SSH）

范围与约束：

- 仅支持 `direct` 工作区模式。
- 远端主机必须是 Linux。
- 会话远程配置持久化路径：`.opencompany/sessions/<session_id>/remote_session.json`。
- 会话配置中不会保存明文密码。

认证与本地依赖：

- `--remote-auth key`：必须提供 `--remote-key-path`。
- `--remote-auth password`：运行时交互输入密码；本机必须安装 `sshpass`。
- `--remote-known-hosts` 支持 `accept_new`（默认）和 `strict`。

后端行为：

- `anthropic`：fail-closed 远程 sandbox 路径，通过 `srt` 执行并施加策略约束。
- `none`：直接在远端 `/bin/bash` 执行，不施加 sandbox 文件/网络约束。

依赖准备（anthropic 后端）：

- 运行时会进行 fail-closed 的依赖检查/准备。
- 会校验或尝试准备 `rg`、`bwrap`、`socat`、`Node.js >= 18`、`srt` 等关键依赖。
- 依赖无法满足时，run/validate 会失败并返回明确错误。

## 🗂️ 日志与持久化

- 会话事件：`.opencompany/sessions/<session_id>/events.jsonl`
- 每个 agent 的消息：`.opencompany/sessions/<session_id>/<agent_id>_messages.jsonl`
- 可选 API 调试追踪：`.opencompany/sessions/<session_id>/debug/<agent_id>__<module>.jsonl`
- 可选调试耗时追踪：`.opencompany/sessions/<session_id>/debug/timings.jsonl`
- 远程会话配置（远程模式）：`.opencompany/sessions/<session_id>/remote_session.json`
- 跨会话诊断日志：`.opencompany/diagnostics.jsonl`

`events.jsonl` / `export.json` / `diagnostics.jsonl` 文件名可在 `[logging]` 中调整。

## 🧱 仓库结构

- `src/opencompany/`：核心 Python 包
- `src/opencompany/webui/`：Web UI 后端与静态前端
- `src/opencompany/tui/`：Textual TUI
- `src/opencompany/tools/`：tool schema 注册与执行器
- `src/opencompany/orchestration/`：agent 运行时与编排流程
- `prompts/`：英文 prompts 与中文镜像
- `docs/`：文档索引、架构、技术路线、消息流文档（含中文镜像）
- `docs/modules/`：子系统模块文档
- `tests/`：单元/集成倾向测试

## 📖 延伸阅读

- `docs/README.md`
- `docs/technical_route.md`
- `docs/architecture.md`
- `docs/message_flow.md`
- `docs/message_stream_map.md`
- `docs/modules/runtime_core.md`
- `docs/modules/tool_runtime.md`
- `docs/modules/ui_surfaces.md`
- 中文镜像：`README_cn.md`、`docs/*_cn.md`
