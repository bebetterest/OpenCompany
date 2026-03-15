# Testing and Debugging Module

## Scope

This module summarizes verification and diagnostics practices for the current refactored runtime.

## Test Surface Overview

Key test groups in `tests/`:

- CLI/config: `test_cli.py`, `test_config.py`
- prompts/protocol/LLM: `test_prompts.py`, `test_openrouter.py`
- orchestration core: `test_orchestrator_loop.py`, `test_orchestrator_finish.py`, `test_orchestrator_resume.py`, `test_orchestrator_tool_runs.py`
- tool runtime and executor: `test_tool_runtime.py`, `test_tools.py`
- message interfaces: `test_messages.py`, `test_message_cursor.py`, `test_message_interface_consistency.py`
- workspace/sandbox: `test_workspace.py`, `test_sandbox.py`
- UI layers: `test_tui.py`, `test_webui.py`, `test_webui_api.py`
- remote direct mode: `test_remote.py` (+ remote branches in `test_cli.py`, `test_orchestrator.py`, `test_sandbox.py`, `test_webui_api.py`)

## Recommended Validation Commands

Use one of the project environment options:

```bash
# Option A: Conda
conda run -n OpenCompany pytest

# Option B: uv (after creating .venv and installing dev deps)
uv run pytest
```

Targeted checks during runtime/tool changes:

```bash
# Option A: Conda
conda run -n OpenCompany pytest tests/test_orchestrator_tool_runs.py tests/test_tool_runtime.py
conda run -n OpenCompany pytest tests/test_orchestrator_resume.py tests/test_message_cursor.py

# Option B: uv
uv run pytest tests/test_orchestrator_tool_runs.py tests/test_tool_runtime.py
uv run pytest tests/test_orchestrator_resume.py tests/test_message_cursor.py
```

## Runtime Debugging Surfaces

- Per-session runtime events: `.opencompany/sessions/<session_id>/events.jsonl`
- Per-agent message logs: `.opencompany/sessions/<session_id>/<agent_id>_messages.jsonl`
- Optional LLM request/response tracing (via `--debug`): `debug/<agent_id>__<module>.jsonl`
- Cross-layer diagnostics: `.opencompany/diagnostics.jsonl`

Useful CLI inspection commands:

```bash
opencompany messages <session_id> --include-extra --format text
opencompany tool-runs <session_id> --status running --limit 200
opencompany tool-run-metrics <session_id>
```

`messages --include-extra` is filtered to non-tool telemetry. Use `tool-runs` for tool call/run lifecycle.

## Debugging Checklist

1. Confirm session and pending tool-run states in storage-derived CLI outputs.
2. Verify whether conversation mismatch is in messages (primary) or only in events (secondary).
3. For resume issues, inspect latest checkpoint + pending tool-run reconstruction behavior.
4. For apply/undo issues, inspect project sync state and backup manifest under session directory.
