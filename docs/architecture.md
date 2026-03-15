# Architecture

## System Components

```mermaid
flowchart LR
    U["User"] --> CLI["CLI (opencompany)\nrun/resume/ui/tui/apply/undo"]
    U --> WEB["Web UI (FastAPI + SPA)"]
    U --> TUI["TUI (Textual fallback)"]

    CLI --> ORCH["Orchestrator"]
    WEB --> ORCH
    TUI --> ORCH

    ORCH --> LOOP["AgentLoopRunner"]
    ORCH --> AR["AgentRuntime\n(ContextAssembler/Store)"]
    ORCH --> TOOLS["ToolExecutor + tool runtime"]
    ORCH --> WS["WorkspaceManager"]
    ORCH --> STORE["Storage (SQLite)"]
    ORCH --> LOGS["JSONL loggers\n(events/messages/diagnostics)"]

    AR --> LLM["OpenRouterClient (streaming)"]
    AR --> PROMPTS["PromptLibrary\n(prompts/*.md|*.json)"]
    TOOLS --> SBOX["Sandbox Backend\n(anthropic | none)"]

    WS --> STAGE["project_sync.json\nstage/apply/undo backups"]
    STORE --> CKPT["checkpoints + tool_runs"]
```

## Runtime Execution Chain

```mermaid
flowchart TD
    A["run_task / resume"] --> B["_run_session"]
    B --> C["_run_root_cycle"]
    C --> D["AgentLoopRunner.run"]
    D --> E["AgentRuntime.ask"]
    E --> F["OpenRouter streaming response"]
    F --> G["_execute_agent_actions"]
    G --> H["_submit_tool_run"]

    H --> I["Read-only / shell tool"]
    H --> J["spawn_agent -> worker loop"]
    H --> K["wait/get/list/cancel tool_run"]
    H --> L["finish"]

    J --> M["_complete_worker\n(diff + parent merge)"]
    M --> N["child summary injection"]
    N --> C

    L --> O["_finalize_root"]
    O --> P["_stage_project_sync"]
    P --> Q["opencompany apply / undo"]

    B --> R["_checkpoint"]
    R --> S["resume: rebuild pending tool runs\n+ pending children"]
    S --> B
```

## Key Architectural Decisions

1. One loop engine for root and worker; role differences are validated in runtime policy (`finish` fields/status).
2. Tool calls are persisted as `tool_runs` instead of ephemeral-only events.
3. Worker file deltas are merged upward before root finalization, then staged before project writeback.
4. Message-first replay source is per-agent `*_messages.jsonl`; runtime events remain secondary observability channels.
5. Resume is checkpoint-driven and reconstructs pending tool runs.

## Boundary and Safety Model

- Worker write scope is its sandbox workspace.
- In `staged` mode, root cannot directly write target project state without explicit user confirmation (`apply`).
- In `direct` mode (local or remote SSH workspace), writes are live under the selected sandbox backend policy (`anthropic` constrained, `none` unconstrained) and there is no staged apply/undo rollback layer.
- `undo` relies on staged backup metadata and copied files from last apply (`staged` mode only).
- Budget exhaustion paths force summary/finalization instead of hidden infinite loops.

## Module References

- `docs/modules/runtime_core.md`
- `docs/modules/orchestration_pipeline.md`
- `docs/modules/tool_runtime.md`
- `docs/modules/workspace_sync.md`
- `docs/modules/persistence_observability.md`
- `docs/modules/llm_prompts.md`
- `docs/modules/ui_surfaces.md`
- `docs/modules/testing_debugging.md`
