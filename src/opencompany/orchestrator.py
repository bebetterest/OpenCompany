from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opencompany.config import OpenCompanyConfig
from opencompany.llm.openrouter import OpenRouterClient
from opencompany.logging import AgentMessageLogger, DiagnosticLogger, StructuredLogger, append_jsonl
from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    CheckpointState,
    EventRecord,
    RootFinalization,
    RemoteSessionConfig,
    RunSession,
    ShellCommandRequest,
    SteerRun,
    SteerRunStatus,
    SessionStatus,
    ToolRun,
    ToolRunStatus,
    WorkspaceMode,
    WorkerCompletion,
    normalize_workspace_mode,
)
from opencompany.remote import (
    build_remote_password_ref,
    delete_remote_session_password,
    delete_remote_session_config,
    load_remote_session_password,
    load_remote_session_config,
    normalize_remote_session_config,
    parse_ssh_target,
    save_remote_session_password,
    save_remote_session_config,
)
from opencompany.orchestration import (
    ActionBatchResult,
    AgentRuntime,
    agent_from_state,
    agent_state,
    root_initial_message,
    session_from_state,
    session_state,
    worker_initial_message,
)
from opencompany.orchestration.context import prompt_window_projection
from opencompany.orchestration.messages import (
    step_limit_summary_message,
)
from opencompany.paths import RuntimePaths
from opencompany.prompts import PromptLibrary
from opencompany.sandbox.anthropic import AnthropicSandboxBackend
from opencompany.sandbox.base import SandboxBackend
from opencompany.sandbox.registry import resolve_sandbox_backend_cls
from opencompany.status_machine import (
    AGENT_ACTIVE_STATUSES,
    AGENT_NON_SCHEDULABLE_STATUSES,
    AGENT_TERMINAL_STATUSES,
    normalize_agent_status,
    normalize_session_completion_state,
    validate_agent_status_transition,
)
from opencompany.storage import Storage
from opencompany.tools import ToolExecutor, child_limit_details, child_summaries, is_descendant
from opencompany.tools.runtime import (
    KNOWN_STEER_RUN_STATUSES,
    PENDING_TOOL_RUN_STATUSES,
    TERMINAL_TOOL_RUN_STATUSES,
    decode_steer_run_cursor,
    decode_tool_run_cursor,
    next_steer_run_cursor,
    next_tool_run_cursor,
    normalize_tool_run_limit,
    parse_steer_run_status_filters,
    parse_tool_run_status_filters,
    steer_run_metrics as build_steer_run_metrics,
    tool_run_duration_ms,
    tool_run_metrics as build_tool_run_metrics,
    validate_compress_context_action,
    validate_finish_action,
    validate_wait_run_action,
    validate_wait_time_action,
)
from opencompany.utils import (
    ensure_directory,
    json_ready,
    load_project_env,
    resolve_in_workspace,
    stable_json_dumps,
    truncate_text,
    utc_now,
)
from opencompany.workspace import WorkspaceChangeSet, WorkspaceManager


def _looks_like_app_dir(path: Path) -> bool:
    return (
        (path / "opencompany.toml").exists()
        and (path / "prompts").is_dir()
        and (path / "src" / "opencompany").is_dir()
    )


def default_app_dir(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if _looks_like_app_dir(candidate):
            return candidate
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_app_dir(candidate):
            return candidate
    raise RuntimeError(
        "Could not determine OpenCompany app directory. Pass app_dir explicitly or run from the OpenCompany repository."
    )


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


PROJECT_SYNC_STATE_VERSION = 1
PROJECT_SYNC_STATE_FILENAME = "project_sync.json"
PROJECT_SYNC_BACKUPS_DIRNAME = "project_sync_backups"
TERMINAL_AGENT_STATUSES = set(AGENT_TERMINAL_STATUSES)
ACTIVE_AGENT_STATUSES = set(AGENT_ACTIVE_STATUSES)
NON_SCHEDULABLE_AGENT_STATUSES = set(AGENT_NON_SCHEDULABLE_STATUSES)
TERMINAL_STEER_RUN_STATUSES = {
    SteerRunStatus.COMPLETED.value,
    SteerRunStatus.CANCELLED.value,
}
USER_STEER_SOURCE_ID = "user"


class Orchestrator:
    def __init__(
        self,
        project_dir: Path,
        locale: str | None = None,
        app_dir: Path | None = None,
        debug: bool = False,
    ) -> None:
        self.app_dir = (app_dir or default_app_dir()).resolve()
        self.project_dir = project_dir.resolve()
        self.debug = bool(debug)
        load_project_env(self.app_dir)
        self.config = OpenCompanyConfig.load(self.app_dir)
        self._base_openrouter_model = str(self.config.llm.openrouter.model)
        self._base_openrouter_coordinator_model = str(self.config.llm.openrouter.coordinator_model)
        self._base_openrouter_worker_model = str(self.config.llm.openrouter.worker_model)
        self._runtime_model_override: str | None = None
        self.locale = self.config.resolve_locale(locale)
        self.paths = RuntimePaths.create(self.app_dir, self.config)
        self.storage = Storage(self.paths.db_path)
        self.diagnostics = DiagnosticLogger(
            self.paths.diagnostics_path(self.config.logging.diagnostics_filename)
        )
        self.prompt_library = PromptLibrary(self.paths.prompts_dir)
        sandbox_backend_cls = resolve_sandbox_backend_cls(self.config.sandbox)
        api_key = self.config.llm.openrouter.api_key
        self.llm_client = (
            OpenRouterClient(
                api_key=api_key,
                base_url=self.config.llm.openrouter.base_url,
                timeout_seconds=self.config.llm.openrouter.timeout_seconds,
                max_retries=self.config.llm.openrouter.max_retries,
                retry_backoff_seconds=self.config.llm.openrouter.retry_backoff_seconds,
                request_response_log_path=None,
            )
            if api_key
            else None
        )
        self.tool_executor = ToolExecutor(
            app_dir=self.app_dir,
            project_dir=self.project_dir,
            config=self.config,
            storage=self.storage,
            sandbox_backend_cls=sandbox_backend_cls,
            log_agent_event=self._log_agent_event,
            log_diagnostic=self._log_diagnostic,
        )
        self.agent_runtime = AgentRuntime(
            config=self.config,
            locale=self.locale,
            prompt_library=self.prompt_library,
            persist_agent=self.storage.upsert_agent,
            log_agent_event=self._log_agent_event,
            append_agent_message=self._append_agent_message,
            append_summary_record=self._append_agent_summary_record,
        )
        self.interrupt_requested = False
        self.latest_session_id: str | None = None
        self._loggers: dict[str, StructuredLogger] = {}
        self._message_loggers: dict[str, AgentMessageLogger] = {}
        self._run_loop_task: asyncio.Task[Any] | None = None
        self._workspace_merge_lock = asyncio.Lock()
        self._worker_semaphore: asyncio.Semaphore | None = None
        self._runtime_wakeup_event: asyncio.Event | None = None
        self._active_root_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_root_failures: list[tuple[str, Exception]] = []
        self._active_worker_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_worker_failures: list[tuple[str, Exception]] = []
        self._active_tool_run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._tool_run_waiters: dict[str, asyncio.Event] = {}
        self._tool_run_shell_streams: dict[str, dict[str, str]] = {}
        self._live_session_contexts: dict[
            str,
            tuple[RunSession, dict[str, AgentNode], WorkspaceManager],
        ] = {}
        self._session_remote_configs: dict[str, RemoteSessionConfig] = {}
        self._log_diagnostic(
            "orchestrator_initialized",
            payload={
                "app_dir": str(self.app_dir),
                "project_dir": str(self.project_dir),
                "locale": self.locale,
                "debug": self.debug,
            },
        )

    def subscribe(self, callback) -> None:
        for logger in self._loggers.values():
            logger.subscribe(callback)
        self._subscriber = callback
        self._log_diagnostic(
            "subscriber_registered",
            payload={"callback": repr(callback), "logger_count": len(self._loggers)},
        )

    @staticmethod
    def _is_terminal_agent(agent: AgentNode) -> bool:
        return agent.status in TERMINAL_AGENT_STATUSES

    @staticmethod
    def _is_active_agent(agent: AgentNode) -> bool:
        return agent.status in ACTIVE_AGENT_STATUSES

    @staticmethod
    def _all_agents_terminal(agents: dict[str, AgentNode]) -> bool:
        if not agents:
            return False
        return all(Orchestrator._is_terminal_agent(node) for node in agents.values())

    @staticmethod
    def _set_agent_status(
        agent: AgentNode,
        new_status: AgentStatus,
        *,
        reason: str | None = None,
        explicit_reopen: bool = False,
    ) -> None:
        previous_status = normalize_agent_status(agent.status)
        target_status = normalize_agent_status(new_status)
        if not validate_agent_status_transition(
            previous=previous_status,
            new=target_status,
            explicit_reopen=explicit_reopen,
        ):
            raise RuntimeError(
                "Invalid agent status transition: "
                f"{previous_status.value} -> {target_status.value} "
                f"(agent_id={agent.id})."
            )
        agent.status = target_status
        if reason is not None:
            agent.status_reason = reason

    @staticmethod
    def _set_session_status(
        session: RunSession,
        new_status: SessionStatus,
        *,
        reason: str | None = None,
        completion_state: str | None = None,
    ) -> None:
        session.status = new_status
        if reason is not None:
            session.status_reason = reason
        raw_completion_state = completion_state if completion_state is not None else session.completion_state
        session.completion_state = normalize_session_completion_state(
            session_status=new_status,
            completion_state=raw_completion_state,
        )

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        return RuntimePaths.normalize_session_id(session_id)

    def request_interrupt(self) -> None:
        self.interrupt_requested = True
        self._signal_runtime_change()
        for task in list(self._active_root_tasks.values()):
            task.cancel()
        for task in list(self._active_worker_tasks.values()):
            task.cancel()
        for task in list(self._active_tool_run_tasks.values()):
            task.cancel()
        self._log_diagnostic(
            "interrupt_requested",
            session_id=self.latest_session_id,
            payload={
                "has_run_loop_task": self._run_loop_task is not None,
                "run_loop_task_done": self._run_loop_task.done() if self._run_loop_task else None,
            },
        )
        current_task = asyncio.current_task()
        if (
            self._run_loop_task
            and self._run_loop_task is not current_task
            and not self._run_loop_task.done()
        ):
            self._run_loop_task.cancel()

    async def run_task(
        self,
        task: str,
        model: str | None = None,
        root_agent_name: str | None = None,
        workspace_mode: WorkspaceMode | str | None = None,
        remote_config: RemoteSessionConfig | dict[str, Any] | None = None,
        remote_password: str | None = None,
    ) -> RunSession:
        selected_model = self._set_runtime_model_override(model)
        resolved_workspace_mode = normalize_workspace_mode(
            workspace_mode or WorkspaceMode.DIRECT.value
        )
        normalized_remote_config = (
            normalize_remote_session_config(remote_config)
            if remote_config is not None
            else None
        )
        if normalized_remote_config is not None and resolved_workspace_mode != WorkspaceMode.DIRECT:
            raise ValueError("Remote workspace is supported only in direct mode.")
        session_id = str(uuid.uuid4())
        self.latest_session_id = session_id
        self.interrupt_requested = False
        self._configure_llm_debug_log(session_id)
        session_dir = self.paths.session_dir(session_id, create=True)
        session_project_dir = (
            Path(normalized_remote_config.remote_dir).expanduser()
            if normalized_remote_config is not None
            else self.project_dir
        )
        self.project_dir = (
            session_project_dir
            if normalized_remote_config is not None
            else session_project_dir.resolve()
        )
        self.tool_executor.set_project_dir(self.project_dir)
        workspace_manager = WorkspaceManager(session_dir)
        now = utc_now()
        session = RunSession(
            id=session_id,
            project_dir=self.project_dir,
            task=task,
            locale=self.locale,
            root_agent_id="",
            workspace_mode=resolved_workspace_mode,
            status=SessionStatus.RUNNING,
            created_at=now,
            updated_at=now,
            config_snapshot=json_ready(asdict(self.config)),
        )
        self._persist_session_remote_config(session.id, normalized_remote_config)
        agents: dict[str, AgentNode] = {}
        root_agent = self._append_root_agent_for_task(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            task=task,
            root_agent_name=root_agent_name,
            status=AgentStatus.RUNNING,
        )
        session.root_agent_id = root_agent.id
        self.storage.upsert_session(session)
        logger = self._get_logger(session_id)
        self._log_diagnostic(
            "session_run_requested",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={
                "task": task,
                "model": selected_model,
                "project_dir": str(self.project_dir),
                "root_agent_id": root_agent.id,
                "workspace_mode": session.workspace_mode.value,
            },
        )
        logger.log(
            session_id=session.id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="session_started",
            phase="runtime",
            payload={
                "task": task,
                "model": selected_model,
                "locale": self.locale,
                "session_status": session.status.value,
                "root_agent_name": root_agent.name,
                "root_agent_role": root_agent.role.value,
                "workspace_mode": session.workspace_mode.value,
            },
            workspace_id=root_agent.workspace_id,
        )
        self._tool_run_waiters = {}
        self._active_tool_run_tasks = {}
        self._tool_run_shell_streams = {}
        self._run_loop_task = asyncio.current_task()
        try:
            self._apply_session_remote_runtime(
                session_id=session.id,
                remote_config=normalized_remote_config,
                remote_password=remote_password,
                require_password=True,
            )
            await self._run_session(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session.status == SessionStatus.RUNNING:
                await self._mark_failed(
                    session=session,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    pending_agent_ids=[],
                    root_loop=0,
                    error=exc,
                )
            raise
        finally:
            if self._run_loop_task is asyncio.current_task():
                self._run_loop_task = None
            self._log_diagnostic(
                "session_run_finished",
                session_id=session.id,
                agent_id=root_agent.id,
                payload={
                    "status": session.status.value,
                    "completion_state": session.completion_state,
                    "loop_index": session.loop_index,
                },
            )
        return session

    async def run_task_in_session(
        self,
        session_id: str,
        task: str,
        model: str | None = None,
        root_agent_name: str | None = None,
        remote_password: str | None = None,
    ) -> RunSession:
        selected_model = self._set_runtime_model_override(model)
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_task = str(task or "").strip()
        if not normalized_task:
            raise ValueError("Task must be non-empty.")

        self.latest_session_id = normalized_session_id
        self.interrupt_requested = False
        self._configure_llm_debug_log(normalized_session_id)
        (
            session,
            agents,
            workspace_manager,
            _pending_agent_ids,
            root_loop,
            checkpoint_seq,
        ) = self._import_session_context(normalized_session_id, source="run")
        remote_config = self._session_remote_config(normalized_session_id)
        self._apply_session_remote_runtime(
            session_id=normalized_session_id,
            remote_config=remote_config,
            remote_password=remote_password,
            require_password=True,
        )

        root_agent = self._append_root_agent_for_task(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            task=normalized_task,
            root_agent_name=root_agent_name,
            status=AgentStatus.RUNNING,
        )
        now = utc_now()
        session.root_agent_id = root_agent.id
        session.task = normalized_task
        self._set_session_status(
            session,
            SessionStatus.RUNNING,
            reason="run_task_in_session_started",
        )
        session.final_summary = None
        session.follow_up_needed = False
        session.updated_at = now
        self.storage.upsert_session(session)

        logger = self._get_logger(normalized_session_id)
        self._log_diagnostic(
            "session_run_requested",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={
                "task": normalized_task,
                "model": selected_model,
                "project_dir": str(self.project_dir),
                "root_agent_id": root_agent.id,
                "checkpoint_seq": checkpoint_seq,
                "workspace_mode": session.workspace_mode.value,
            },
        )
        logger.log(
            session_id=session.id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="session_started",
            phase="runtime",
            payload={
                "task": normalized_task,
                "model": selected_model,
                "locale": self.locale,
                "session_status": session.status.value,
                "root_agent_name": root_agent.name,
                "root_agent_role": root_agent.role.value,
                "checkpoint_seq": checkpoint_seq,
                "workspace_mode": session.workspace_mode.value,
            },
            workspace_id=root_agent.workspace_id,
        )
        self._tool_run_waiters = {}
        self._active_tool_run_tasks = {}
        self._tool_run_shell_streams = {}
        self._run_loop_task = asyncio.current_task()
        try:
            await self._run_session(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=[],
                root_loop=max(0, int(root_loop)),
            )
        finally:
            if self._run_loop_task is asyncio.current_task():
                self._run_loop_task = None
            self._log_diagnostic(
                "session_run_finished",
                session_id=session.id,
                agent_id=root_agent.id,
                payload={
                    "status": session.status.value,
                    "completion_state": session.completion_state,
                    "loop_index": session.loop_index,
                },
            )
        return session

    def submit_run_in_active_session(
        self,
        session_id: str,
        task: str,
        *,
        model: str | None = None,
        root_agent_name: str | None = None,
        source: str = "webui",
    ) -> dict[str, Any]:
        selected_model = self._set_runtime_model_override(model)
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_task = str(task or "").strip()
        if not normalized_task:
            raise ValueError("Task must be non-empty.")

        runtime_context = self._live_session_contexts.get(normalized_session_id)
        if runtime_context is None:
            raise RuntimeError(
                f"Session {normalized_session_id} is not active in the current runtime."
            )
        session, agents, workspace_manager = runtime_context
        if session.status != SessionStatus.RUNNING:
            raise RuntimeError(
                f"Session {normalized_session_id} is not running (status={session.status.value})."
            )

        root_agent = self._append_root_agent_for_task(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            task=normalized_task,
            root_agent_name=root_agent_name,
            status=AgentStatus.RUNNING,
        )
        session.root_agent_id = root_agent.id
        session.task = normalized_task
        session.updated_at = utc_now()
        self.storage.upsert_session(session)

        logger = self._get_logger(normalized_session_id)
        self._log_diagnostic(
            "session_run_requested_while_running",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={
                "task": normalized_task,
                "model": selected_model,
                "source": str(source or "manual"),
                "project_dir": str(self.project_dir),
                "root_agent_id": root_agent.id,
                "workspace_mode": session.workspace_mode.value,
            },
        )
        logger.log(
            session_id=session.id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="session_started",
            phase="runtime",
            payload={
                "task": normalized_task,
                "model": selected_model,
                "locale": self.locale,
                "session_status": session.status.value,
                "root_agent_name": root_agent.name,
                "root_agent_role": root_agent.role.value,
                "source": str(source or "manual"),
                "running_session_append": True,
                "workspace_mode": session.workspace_mode.value,
            },
            workspace_id=root_agent.workspace_id,
        )
        return {
            "session_id": session.id,
            "root_agent_id": root_agent.id,
            "task": normalized_task,
            "model": selected_model,
            "source": str(source or "manual"),
            "workspace_mode": session.workspace_mode.value,
        }

    def _append_root_agent_for_task(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        task: str,
        root_agent_name: str | None = None,
        status: AgentStatus = AgentStatus.RUNNING,
    ) -> AgentNode:
        normalized_task = str(task or "").strip()
        if not normalized_task:
            raise ValueError("Task must be non-empty.")
        root_workspace_id = self._root_workspace_id_for_session(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
        )
        normalized_root_agent_name = str(root_agent_name or "").strip() or "Root Coordinator"
        now = utc_now()
        root_agent = AgentNode(
            id=self._new_unique_agent_id(agents),
            session_id=session.id,
            name=self._unique_agent_name(normalized_root_agent_name, agents),
            role=AgentRole.ROOT,
            instruction=normalized_task,
            workspace_id=root_workspace_id,
            status=status,
            metadata={
                "created_at": now,
                "model": self.config.llm.openrouter.model_for_role(AgentRole.ROOT.value),
            },
        )
        root_agent.conversation = [
            {
                "role": "user",
                "content": self._with_agent_identity_prompt(
                    agent_name=root_agent.name,
                    agent_id=root_agent.id,
                    parent_agent_name=None,
                    parent_agent_id=None,
                    content=self._root_initial_message(normalized_task),
                ),
            }
        ]
        agents[root_agent.id] = root_agent
        self.storage.upsert_agent(root_agent)
        self._sync_agent_messages(root_agent)
        return root_agent

    @staticmethod
    def _unique_agent_name(base_name: str, agents: dict[str, AgentNode]) -> str:
        normalized_base = str(base_name or "").strip() or "Agent"
        used = {
            str(node.name or "").strip().casefold()
            for node in agents.values()
            if str(node.name or "").strip()
        }
        if normalized_base.casefold() not in used:
            return normalized_base
        suffix = 2
        while True:
            candidate = f"{normalized_base} ({suffix})"
            if candidate.casefold() not in used:
                return candidate
            suffix += 1

    def _with_agent_identity_prompt(
        self,
        *,
        agent_name: str,
        agent_id: str,
        parent_agent_name: str | None = None,
        parent_agent_id: str | None = None,
        content: str,
    ) -> str:
        identity = self._agent_identity_block(
            agent_name=agent_name,
            agent_id=agent_id,
            parent_agent_name=parent_agent_name,
            parent_agent_id=parent_agent_id,
        ).strip()
        body = str(content or "")
        if not identity:
            return body
        if body.startswith(identity):
            return body
        return f"{identity}\n{body}" if body else identity

    def _agent_identity_block(
        self,
        *,
        agent_name: str,
        agent_id: str,
        parent_agent_name: str | None = None,
        parent_agent_id: str | None = None,
    ) -> str:
        identity = self._agent_identity_line(agent_name=agent_name, agent_id=agent_id).strip()
        parent = self._parent_agent_identity_line(
            parent_agent_name=parent_agent_name,
            parent_agent_id=parent_agent_id,
        ).strip()
        return "\n".join(line for line in (identity, parent) if line)

    def _agent_identity_line(self, *, agent_name: str, agent_id: str) -> str:
        normalized_name = str(agent_name or "").strip() or "Agent"
        normalized_id = str(agent_id or "").strip()
        if self.locale == "zh":
            return f"你是{normalized_name}(agent id: {normalized_id})"
        return f"You are {normalized_name} (agent id: {normalized_id})."

    def _parent_agent_identity_line(
        self,
        *,
        parent_agent_name: str | None,
        parent_agent_id: str | None,
    ) -> str:
        normalized_parent_name = str(parent_agent_name or "").strip()
        normalized_parent_id = str(parent_agent_id or "").strip()
        if normalized_parent_name and normalized_parent_id:
            if self.locale == "zh":
                return f"你的父agent是{normalized_parent_name}(agent id: {normalized_parent_id})"
            return f"Your parent agent is {normalized_parent_name} (agent id: {normalized_parent_id})."
        if self.locale == "zh":
            return "你的父agent为空。"
        return "You have no parent agent."

    def _root_workspace_id_for_session(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> str:
        root_workspace_id = ""
        previous_root = agents.get(session.root_agent_id)
        if previous_root is not None:
            root_workspace_id = str(previous_root.workspace_id).strip()
        if not root_workspace_id:
            for node in sorted(agents.values(), key=lambda item: item.id):
                if node.role == AgentRole.ROOT and str(node.workspace_id).strip():
                    root_workspace_id = str(node.workspace_id).strip()
                    break
        if root_workspace_id:
            return root_workspace_id
        root_workspace = workspace_manager.create_root_workspace(
            session.project_dir,
            mode=session.workspace_mode,
        )
        return root_workspace.id

    @staticmethod
    def _is_direct_workspace_mode(session: RunSession) -> bool:
        return normalize_workspace_mode(session.workspace_mode) == WorkspaceMode.DIRECT

    def _session_remote_config(
        self,
        session_id: str,
        *,
        load_if_missing: bool = True,
    ) -> RemoteSessionConfig | None:
        normalized = self._normalize_session_id(session_id)
        cached = self._session_remote_configs.get(normalized)
        if cached is not None:
            return cached
        if not load_if_missing:
            return None
        session_dir = self.paths.session_dir(normalized, create=False)
        loaded = load_remote_session_config(session_dir)
        if loaded is not None:
            self._session_remote_configs[normalized] = loaded
        return loaded

    def _persist_session_remote_config(
        self,
        session_id: str,
        config: RemoteSessionConfig | None,
    ) -> None:
        normalized = self._normalize_session_id(session_id)
        session_dir = self.paths.session_dir(normalized, create=True)
        if config is None:
            existing = self._session_remote_configs.get(normalized)
            if existing is None:
                existing = load_remote_session_config(session_dir)
            if existing is not None and str(existing.password_ref or "").strip():
                with suppress(Exception):
                    delete_remote_session_password(existing.password_ref)
            delete_remote_session_config(session_dir)
            self._session_remote_configs.pop(normalized, None)
            self.tool_executor.clear_session_remote_context(normalized)
            return
        save_remote_session_config(session_dir, config)
        self._session_remote_configs[normalized] = config

    def _apply_session_remote_runtime(
        self,
        *,
        session_id: str,
        remote_config: RemoteSessionConfig | None,
        remote_password: str | None = None,
        require_password: bool = False,
    ) -> None:
        normalized = self._normalize_session_id(session_id)
        if remote_config is None:
            self.tool_executor.clear_session_remote_context(normalized)
            return
        password = str(remote_password or "").strip()
        if remote_config.auth_mode == "password":
            existing = self.tool_executor.session_remote_context(normalized)
            if not password and existing is not None:
                password = str(existing.password or "").strip()
            if not password and str(remote_config.password_ref or "").strip():
                password = str(load_remote_session_password(remote_config.password_ref) or "").strip()
            if password:
                password_ref = str(remote_config.password_ref or "").strip()
                if not password_ref:
                    password_ref = build_remote_password_ref(normalized, remote_config)
                    remote_config.password_ref = password_ref
                    self._persist_session_remote_config(normalized, remote_config)
                try:
                    save_remote_session_password(password_ref, password)
                except Exception as exc:
                    self._log_diagnostic(
                        "remote_password_persist_failed",
                        level="warning",
                        session_id=normalized,
                        payload={
                            "ssh_target": remote_config.ssh_target,
                            "error": str(exc),
                            "error_type": exc.__class__.__name__,
                        },
                    )
            if require_password and not password:
                raise ValueError(
                    "Remote password is required for this session. Provide remote_password when starting/resuming/terminal."
                )
        else:
            if str(remote_config.password_ref or "").strip():
                with suppress(Exception):
                    delete_remote_session_password(remote_config.password_ref)
                remote_config.password_ref = ""
                self._persist_session_remote_config(normalized, remote_config)
            password = ""
        self.tool_executor.set_session_remote_config(
            normalized,
            remote_config,
            password=password,
        )

    def load_session_context(self, session_id: str) -> RunSession:
        normalized_session_id = self._normalize_session_id(session_id)
        cloned_session_id = self._clone_session_context(normalized_session_id)
        session, _, _, _, _, _ = self._import_session_context(
            cloned_session_id,
            source="reconfigure",
        )
        return session

    async def resume(
        self,
        session_id: str,
        instruction: str,
        model: str | None = None,
        reactivate_agent_id: str | None = None,
        run_root_agent: bool = True,
        remote_password: str | None = None,
    ) -> RunSession:
        selected_model = self._set_runtime_model_override(model)
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_instruction = str(instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("Resume instruction must be non-empty.")
        normalized_reactivate_agent_id = str(reactivate_agent_id or "").strip() or None
        normalized_run_root_agent = bool(run_root_agent)

        self.latest_session_id = normalized_session_id
        self.interrupt_requested = False
        self._configure_llm_debug_log(normalized_session_id)
        (
            session,
            agents,
            workspace_manager,
            pending_agent_ids,
            root_loop,
            checkpoint_seq,
        ) = self._import_session_context(normalized_session_id, source="resume")
        remote_config = self._session_remote_config(normalized_session_id)
        self._apply_session_remote_runtime(
            session_id=normalized_session_id,
            remote_config=remote_config,
            remote_password=remote_password,
            require_password=True,
        )
        if normalized_run_root_agent and normalized_reactivate_agent_id:
            candidate = agents.get(normalized_reactivate_agent_id)
            if candidate is not None and candidate.role == AgentRole.ROOT:
                session.root_agent_id = candidate.id
        root_agent = agents.get(session.root_agent_id)
        if root_agent is None:
            raise ValueError(
                f"Root agent {session.root_agent_id} is missing for session {normalized_session_id}."
            )
        focus_agent = (
            agents.get(normalized_reactivate_agent_id)
            if normalized_reactivate_agent_id
            else None
        )
        if (
            not normalized_run_root_agent
            and (
                normalized_reactivate_agent_id is None
                or normalized_reactivate_agent_id == session.root_agent_id
                or (focus_agent is not None and focus_agent.role == AgentRole.ROOT)
            )
        ):
            raise ValueError(
                "run_root_agent=False requires reactivate_agent_id to reference a non-root agent."
            )
        self._reactivate_agent_in_resume_context_if_needed(
            session=session,
            agents=agents,
            agent_id=normalized_reactivate_agent_id,
        )
        if normalized_run_root_agent:
            self._reactivate_agent_in_resume_context_if_needed(
                session=session,
                agents=agents,
                agent_id=session.root_agent_id,
            )
            self._append_agent_message(
                root_agent,
                {"role": "user", "content": normalized_instruction},
            )
        self.tool_executor.set_project_dir(self.project_dir)
        previous_config_snapshot = (
            dict(session.config_snapshot)
            if isinstance(session.config_snapshot, dict)
            else {}
        )
        refreshed_config_snapshot = json_ready(asdict(self.config))
        for key, value in previous_config_snapshot.items():
            if key not in refreshed_config_snapshot:
                refreshed_config_snapshot[key] = value
        session.config_snapshot = refreshed_config_snapshot
        self._set_session_status(
            session,
            SessionStatus.RUNNING,
            reason="session_resumed",
        )
        session.updated_at = utc_now()
        self.storage.upsert_session(session)

        self._tool_run_waiters = {}
        self._active_tool_run_tasks = {}
        self._tool_run_shell_streams = {}
        logger = self._get_logger(normalized_session_id)
        self._log_diagnostic(
            "session_resume_requested",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "checkpoint_seq": checkpoint_seq,
                "project_dir": str(self.project_dir),
                "instruction": normalized_instruction,
                "model": selected_model,
                "reactivate_agent_id": normalized_reactivate_agent_id,
                "run_root_agent": normalized_run_root_agent,
            },
        )
        logger.log(
            session_id=session.id,
            agent_id=session.root_agent_id,
            parent_agent_id=None,
            event_type="session_resumed",
            phase="runtime",
            payload={
                "checkpoint_seq": checkpoint_seq,
                "task": session.task,
                "session_status": session.status.value,
                "root_agent_name": root_agent.name,
                "root_agent_role": root_agent.role.value,
                "instruction": normalized_instruction,
                "model": selected_model,
                "reactivate_agent_id": normalized_reactivate_agent_id,
                "run_root_agent": normalized_run_root_agent,
            },
            workspace_id=root_agent.workspace_id,
        )
        self._run_loop_task = asyncio.current_task()
        try:
            await self._run_session(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=pending_agent_ids,
                root_loop=root_loop,
                run_root_agent=normalized_run_root_agent,
                focus_agent_id=(
                    None if normalized_run_root_agent else normalized_reactivate_agent_id
                ),
            )
        finally:
            if self._run_loop_task is asyncio.current_task():
                self._run_loop_task = None
            self._log_diagnostic(
                "session_resume_finished",
                session_id=session.id,
                agent_id=session.root_agent_id,
                payload={
                    "status": session.status.value,
                    "completion_state": session.completion_state,
                    "loop_index": session.loop_index,
                },
            )
        return session

    def _clone_session_context(self, source_session_id: str) -> str:
        normalized_source_session_id = self._normalize_session_id(source_session_id)
        source_checkpoint = self.storage.latest_checkpoint(normalized_source_session_id)
        if not source_checkpoint:
            raise ValueError(f"No checkpoint found for session {normalized_source_session_id}")
        source_state = source_checkpoint.get("state", {})
        if not isinstance(source_state, dict):
            raise ValueError(f"Checkpoint payload is invalid for session {normalized_source_session_id}")
        source_session_payload = source_state.get("session")
        if not isinstance(source_session_payload, dict):
            raise ValueError(
                f"Session payload is missing in checkpoint for session {normalized_source_session_id}"
            )
        source_session = self._session_from_state(source_session_payload)
        stored_source_session = self.storage.load_session(normalized_source_session_id)
        if stored_source_session is not None:
            source_session = self._session_from_storage_row(
                stored_source_session,
                fallback=source_session,
            )

        source_session_dir = self.paths.existing_session_dir(normalized_source_session_id).resolve()
        cloned_session_id = str(uuid.uuid4())
        cloned_session_dir = self.paths.session_dir(cloned_session_id, create=False).resolve()
        if cloned_session_dir.exists():
            shutil.rmtree(cloned_session_dir)
        shutil.copytree(source_session_dir, cloned_session_dir)
        self._rewrite_cloned_message_logs(cloned_session_dir, cloned_session_id)

        now = utc_now()
        source_config_snapshot = (
            dict(source_session.config_snapshot)
            if isinstance(source_session.config_snapshot, dict)
            else {}
        )
        source_config_snapshot["continued_from_session_id"] = normalized_source_session_id
        source_config_snapshot["continued_from_checkpoint_seq"] = int(source_checkpoint["seq"])
        cloned_session = RunSession(
            id=cloned_session_id,
            project_dir=source_session.project_dir,
            task=source_session.task,
            locale=source_session.locale,
            root_agent_id=source_session.root_agent_id,
            workspace_mode=source_session.workspace_mode,
            status=source_session.status,
            status_reason=source_session.status_reason,
            created_at=now,
            updated_at=now,
            loop_index=source_session.loop_index,
            final_summary=source_session.final_summary,
            completion_state=source_session.completion_state,
            follow_up_needed=source_session.follow_up_needed,
            config_snapshot=source_config_snapshot,
        )
        self.storage.upsert_session(cloned_session)

        tool_run_id_map = self._clone_tool_runs_for_cloned_session(
            source_session_id=normalized_source_session_id,
            cloned_session_id=cloned_session_id,
        )
        steer_run_id_map = self._clone_steer_runs_for_cloned_session(
            source_session_id=normalized_source_session_id,
            cloned_session_id=cloned_session_id,
        )
        self._rewrite_cloned_message_logs(
            cloned_session_dir,
            cloned_session_id,
            tool_run_id_map=tool_run_id_map,
            steer_run_id_map=steer_run_id_map,
        )
        source_checkpoints = self.storage.load_checkpoints(normalized_source_session_id)
        if not source_checkpoints:
            raise ValueError(f"No checkpoint found for session {normalized_source_session_id}")
        for checkpoint in source_checkpoints:
            rewritten_state = self._rewrite_checkpoint_for_cloned_session(
                state=checkpoint.get("state", {}),
                source_session_id=normalized_source_session_id,
                cloned_session_id=cloned_session_id,
                source_session_dir=source_session_dir,
                cloned_session_dir=cloned_session_dir,
                tool_run_id_map=tool_run_id_map,
                steer_run_id_map=steer_run_id_map,
            )
            checkpoint_created_at = str(checkpoint.get("created_at", "")).strip() or now
            self.storage.save_checkpoint(cloned_session_id, checkpoint_created_at, rewritten_state)

        for row in self.storage.load_events(normalized_source_session_id):
            payload = row.get("payload_json", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload = self._rewrite_run_ids_in_payload(
                payload,
                tool_run_id_map=tool_run_id_map,
                steer_run_id_map=steer_run_id_map,
            )
            self.storage.append_event(
                EventRecord(
                    timestamp=str(row.get("timestamp", "")).strip() or now,
                    session_id=cloned_session_id,
                    agent_id=(
                        str(row.get("agent_id", "")).strip() or None
                    ),
                    parent_agent_id=(
                        str(row.get("parent_agent_id", "")).strip() or None
                    ),
                    event_type=str(row.get("event_type", "")),
                    phase=str(row.get("phase", "runtime")),
                    payload=payload,
                    workspace_id=(
                        str(row.get("workspace_id", "")).strip() or None
                    ),
                    checkpoint_seq=int(row.get("checkpoint_seq", 0) or 0),
                )
            )

        self._log_diagnostic(
            "session_context_cloned",
            session_id=cloned_session_id,
            agent_id=cloned_session.root_agent_id,
            payload={
                "source_session_id": normalized_source_session_id,
                "source_checkpoint_seq": int(source_checkpoint["seq"]),
            },
        )
        return cloned_session_id

    def _clone_tool_runs_for_cloned_session(
        self,
        *,
        source_session_id: str,
        cloned_session_id: str,
    ) -> dict[str, str]:
        source_runs = self.storage.load_tool_runs_for_session(source_session_id)
        run_id_map: dict[str, str] = {}
        for run in source_runs:
            source_run_id = str(run.get("id", "")).strip()
            if not source_run_id:
                continue
            run_id_map[source_run_id] = self._new_tool_run_id()
        for run in source_runs:
            source_run_id = str(run.get("id", "")).strip()
            mapped_run_id = run_id_map.get(source_run_id)
            if not mapped_run_id:
                continue
            raw_status = str(run.get("status", ToolRunStatus.CANCELLED.value)).strip().lower()
            try:
                status = ToolRunStatus(raw_status)
            except ValueError:
                status = ToolRunStatus.CANCELLED
            parent_run_id = str(run.get("parent_run_id", "")).strip()
            mapped_parent_run_id = run_id_map.get(parent_run_id) if parent_run_id else None
            arguments = run.get("arguments")
            cloned_arguments = dict(arguments) if isinstance(arguments, dict) else {}
            result = run.get("result")
            cloned_result = dict(result) if isinstance(result, dict) else None
            error = run.get("error")
            self.storage.upsert_tool_run(
                ToolRun(
                    id=mapped_run_id,
                    session_id=cloned_session_id,
                    agent_id=str(run.get("agent_id", "")),
                    tool_name=str(run.get("tool_name", "")),
                    arguments=cloned_arguments,
                    status=status,
                    blocking=bool(run.get("blocking", False)),
                    status_reason=(
                        str(run.get("status_reason"))
                        if run.get("status_reason") is not None
                        else None
                    ),
                    parent_run_id=mapped_parent_run_id,
                    result=cloned_result,
                    error=str(error) if error is not None else None,
                    created_at=str(run.get("created_at", "")),
                    started_at=(
                        str(run.get("started_at"))
                        if run.get("started_at") is not None
                        else None
                    ),
                    completed_at=(
                        str(run.get("completed_at"))
                        if run.get("completed_at") is not None
                        else None
                    ),
                )
            )
        return run_id_map

    def _clone_steer_runs_for_cloned_session(
        self,
        *,
        source_session_id: str,
        cloned_session_id: str,
    ) -> dict[str, str]:
        source_runs = self.storage.load_steer_runs_for_session(source_session_id)
        run_id_map: dict[str, str] = {}
        for run in source_runs:
            source_run_id = str(run.get("id", "")).strip()
            if not source_run_id:
                continue
            run_id_map[source_run_id] = self._new_steer_run_id()
        for run in source_runs:
            source_run_id = str(run.get("id", "")).strip()
            mapped_run_id = run_id_map.get(source_run_id)
            if not mapped_run_id:
                continue
            raw_status = str(run.get("status", SteerRunStatus.CANCELLED.value)).strip().lower()
            try:
                status = SteerRunStatus(raw_status)
            except ValueError:
                status = SteerRunStatus.CANCELLED
            raw_delivered_step = run.get("delivered_step")
            try:
                delivered_step = (
                    int(raw_delivered_step)
                    if raw_delivered_step is not None
                    else None
                )
            except (TypeError, ValueError):
                delivered_step = None
            self.storage.upsert_steer_run(
                SteerRun(
                    id=mapped_run_id,
                    session_id=cloned_session_id,
                    agent_id=str(run.get("agent_id", "")),
                    content=str(run.get("content", "") or ""),
                    source=str(run.get("source", "") or ""),
                    status=status,
                    source_agent_id=(
                        str(run.get("source_agent_id", "")).strip() or USER_STEER_SOURCE_ID
                    ),
                    source_agent_name=(
                        str(run.get("source_agent_name", "")).strip()
                        or self._steer_user_label()
                    ),
                    status_reason=(
                        str(run.get("status_reason"))
                        if run.get("status_reason") is not None
                        else None
                    ),
                    created_at=str(run.get("created_at", "")),
                    completed_at=(
                        str(run.get("completed_at"))
                        if run.get("completed_at") is not None
                        else None
                    ),
                    cancelled_at=(
                        str(run.get("cancelled_at"))
                        if run.get("cancelled_at") is not None
                        else None
                    ),
                    delivered_step=delivered_step,
                )
            )
        return run_id_map

    def _rewrite_run_ids_in_payload(
        self,
        payload: dict[str, Any],
        *,
        tool_run_id_map: dict[str, str],
        steer_run_id_map: dict[str, str],
    ) -> dict[str, Any]:
        if not tool_run_id_map and not steer_run_id_map:
            return payload
        tool_key_names = {"tool_run_id", "parent_run_id"}
        steer_key_names = {"steer_run_id"}

        def rewrite(value: Any, *, key: str | None = None) -> Any:
            if isinstance(value, dict):
                return {
                    str(child_key): rewrite(child_value, key=str(child_key))
                    for child_key, child_value in value.items()
                }
            if isinstance(value, list):
                return [rewrite(item, key=None) for item in value]
            if key in tool_key_names and isinstance(value, str):
                return tool_run_id_map.get(value, value)
            if key in steer_key_names and isinstance(value, str):
                return steer_run_id_map.get(value, value)
            return value

        rewritten = rewrite(payload)
        return rewritten if isinstance(rewritten, dict) else payload

    def _rewrite_cloned_message_logs(
        self,
        session_dir: Path,
        session_id: str,
        *,
        tool_run_id_map: dict[str, str] | None = None,
        steer_run_id_map: dict[str, str] | None = None,
    ) -> None:
        normalized_tool_map = tool_run_id_map or {}
        normalized_steer_map = steer_run_id_map or {}
        for path in sorted(session_dir.glob("*_messages.jsonl")):
            rewritten_lines: list[str] = []
            changed = False
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw_line = line.strip()
                    if not raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        rewritten_lines.append(raw_line)
                        continue
                    if not isinstance(record, dict):
                        rewritten_lines.append(raw_line)
                        continue
                    if str(record.get("session_id", "")).strip() != session_id:
                        record["session_id"] = session_id
                        changed = True
                    if normalized_tool_map or normalized_steer_map:
                        rewritten_record = self._rewrite_run_ids_in_payload(
                            record,
                            tool_run_id_map=normalized_tool_map,
                            steer_run_id_map=normalized_steer_map,
                        )
                        if rewritten_record != record:
                            changed = True
                            record = rewritten_record
                    rewritten_lines.append(
                        json.dumps(json_ready(record), ensure_ascii=False)
                    )
            if changed:
                payload = "\n".join(rewritten_lines)
                if payload:
                    payload += "\n"
                path.write_text(payload, encoding="utf-8")

    def _rewrite_checkpoint_for_cloned_session(
        self,
        *,
        state: dict[str, Any],
        source_session_id: str,
        cloned_session_id: str,
        source_session_dir: Path,
        cloned_session_dir: Path,
        tool_run_id_map: dict[str, str],
        steer_run_id_map: dict[str, str],
    ) -> dict[str, Any]:
        del steer_run_id_map
        serialized = stable_json_dumps(state if isinstance(state, dict) else {})
        rewritten_state = json.loads(serialized)
        if not isinstance(rewritten_state, dict):
            rewritten_state = {}

        session_payload = rewritten_state.get("session")
        rewritten_workspace_mode = WorkspaceMode.STAGED
        rewritten_project_dir = cloned_session_dir
        if isinstance(session_payload, dict):
            session_payload["id"] = cloned_session_id
            config_snapshot = session_payload.get("config_snapshot")
            if not isinstance(config_snapshot, dict):
                config_snapshot = {}
                session_payload["config_snapshot"] = config_snapshot
            config_snapshot["continued_from_session_id"] = source_session_id
            rewritten_workspace_mode = normalize_workspace_mode(
                session_payload.get("workspace_mode")
            )
            rewritten_project_dir = Path(
                str(session_payload.get("project_dir", cloned_session_dir))
            ).resolve()

        agents_payload = rewritten_state.get("agents")
        if isinstance(agents_payload, dict):
            for payload in agents_payload.values():
                if not isinstance(payload, dict):
                    continue
                payload["session_id"] = cloned_session_id

        workspaces_payload = rewritten_state.get("workspaces")
        if isinstance(workspaces_payload, dict):
            for workspace_key, payload in workspaces_payload.items():
                if not isinstance(payload, dict):
                    continue
                workspace_id = str(payload.get("id", workspace_key)).strip() or str(workspace_key)
                payload["id"] = workspace_id
                remapped_path = self._remap_workspace_path_for_cloned_session(
                    payload.get("path"),
                    source_session_dir=source_session_dir,
                    cloned_session_dir=cloned_session_dir,
                )
                payload["path"] = str(
                    self._resolve_workspace_path_for_session(
                        session_dir=cloned_session_dir,
                        workspace_id=workspace_id,
                        raw_path=remapped_path,
                        field_name="path",
                        workspace_mode=rewritten_workspace_mode,
                        project_dir=rewritten_project_dir,
                    )
                )
                remapped_base_snapshot_path = self._remap_workspace_path_for_cloned_session(
                    payload.get("base_snapshot_path"),
                    source_session_dir=source_session_dir,
                    cloned_session_dir=cloned_session_dir,
                )
                payload["base_snapshot_path"] = str(
                    self._resolve_workspace_path_for_session(
                        session_dir=cloned_session_dir,
                        workspace_id=workspace_id,
                        raw_path=remapped_base_snapshot_path,
                        field_name="base_snapshot_path",
                        workspace_mode=rewritten_workspace_mode,
                        project_dir=rewritten_project_dir,
                    )
                )

        pending_tool_run_ids: list[str] = []
        raw_pending_tool_run_ids = rewritten_state.get("pending_tool_run_ids")
        if isinstance(raw_pending_tool_run_ids, list):
            for pending_tool_run_id in raw_pending_tool_run_ids:
                normalized = str(pending_tool_run_id or "").strip()
                if not normalized:
                    continue
                mapped = tool_run_id_map.get(normalized)
                if mapped:
                    pending_tool_run_ids.append(mapped)
        rewritten_state["pending_tool_run_ids"] = pending_tool_run_ids
        return rewritten_state

    def _remap_workspace_path_for_cloned_session(
        self,
        raw_path: Any,
        *,
        source_session_dir: Path,
        cloned_session_dir: Path,
    ) -> Path:
        raw_text = str(raw_path or "").strip()
        if not raw_text:
            return cloned_session_dir
        source_root = source_session_dir.resolve()
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = source_root / candidate
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(source_root)
        except ValueError:
            return candidate
        return (cloned_session_dir / relative).resolve()

    def _infer_workspace_path_for_session(
        self,
        *,
        session_dir: Path,
        workspace_id: str,
        field_name: str,
    ) -> Path | None:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            return None
        normalized_field_name = str(field_name or "").strip()
        if normalized_workspace_id == "root":
            if normalized_field_name == "base_snapshot_path":
                return (session_dir / "snapshots" / "root_base").resolve()
            return (session_dir / "snapshots" / "root").resolve()
        if normalized_workspace_id.startswith("ws-"):
            agent_id = normalized_workspace_id[3:].strip()
            if agent_id:
                if normalized_field_name == "base_snapshot_path":
                    return (session_dir / "snapshots" / f"{agent_id}_base").resolve()
                return (session_dir / "workspaces" / agent_id).resolve()
        return None

    def _resolve_workspace_path_for_session(
        self,
        *,
        session_dir: Path,
        workspace_id: str,
        raw_path: Any,
        field_name: str,
        workspace_mode: WorkspaceMode | str | None = None,
        project_dir: Path | None = None,
    ) -> Path:
        normalized_session_dir = session_dir.resolve()
        normalized_workspace_mode = normalize_workspace_mode(workspace_mode)
        inferred_path = self._infer_workspace_path_for_session(
            session_dir=normalized_session_dir,
            workspace_id=workspace_id,
            field_name=field_name,
        )
        if (
            normalized_workspace_mode == WorkspaceMode.DIRECT
            and workspace_id == "root"
            and field_name in {"path", "base_snapshot_path"}
            and project_dir is not None
        ):
            # Direct sessions should always bind root/base to the live project.
            # For remote-direct sessions this path may not exist locally.
            return project_dir
        raw_text = str(raw_path or "").strip()
        candidate: Path | None = None
        if raw_text:
            candidate = Path(raw_text)
            if not candidate.is_absolute():
                candidate = (normalized_session_dir / candidate).resolve()
            else:
                candidate = candidate.resolve()
            if inferred_path is not None and inferred_path.exists():
                return inferred_path
            if candidate.exists():
                return candidate
        if inferred_path is not None:
            return inferred_path
        if candidate is not None:
            return candidate
        return normalized_session_dir

    @staticmethod
    def _normalize_workspace_project_dir(
        project_dir: Path,
        *,
        workspace_mode: WorkspaceMode | str | None,
    ) -> Path:
        normalized_mode = normalize_workspace_mode(workspace_mode)
        expanded = project_dir.expanduser()
        if (
            normalized_mode == WorkspaceMode.DIRECT
            and expanded.is_absolute()
            and not expanded.exists()
        ):
            return expanded
        return expanded.resolve()

    def _normalize_workspace_state_for_session(
        self,
        *,
        session_id: str,
        workspace_mode: WorkspaceMode | str | None,
        project_dir: Path,
        workspaces_state: Any,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(workspaces_state, dict):
            raise ValueError(f"Checkpoint workspace payload is invalid for session {session_id}.")
        session_dir = self.paths.session_dir(session_id, create=False).resolve()
        normalized_workspace_mode = normalize_workspace_mode(workspace_mode)
        resolved_project_dir = self._normalize_workspace_project_dir(
            project_dir,
            workspace_mode=normalized_workspace_mode,
        )
        normalized_state: dict[str, dict[str, Any]] = {}
        for workspace_key, raw_payload in workspaces_state.items():
            if not isinstance(raw_payload, dict):
                continue
            workspace_id = str(raw_payload.get("id", workspace_key)).strip() or str(workspace_key).strip()
            if not workspace_id:
                continue
            payload = dict(raw_payload)
            payload["id"] = workspace_id
            payload["path"] = str(
                self._resolve_workspace_path_for_session(
                    session_dir=session_dir,
                    workspace_id=workspace_id,
                    raw_path=payload.get("path"),
                    field_name="path",
                    workspace_mode=normalized_workspace_mode,
                    project_dir=resolved_project_dir,
                )
            )
            payload["base_snapshot_path"] = str(
                self._resolve_workspace_path_for_session(
                    session_dir=session_dir,
                    workspace_id=workspace_id,
                    raw_path=payload.get("base_snapshot_path"),
                    field_name="base_snapshot_path",
                    workspace_mode=normalized_workspace_mode,
                    project_dir=resolved_project_dir,
                )
            )
            parent_workspace_id = payload.get("parent_workspace_id")
            if parent_workspace_id is None:
                payload["parent_workspace_id"] = None
            else:
                payload["parent_workspace_id"] = str(parent_workspace_id).strip() or None
            payload["readonly"] = bool(payload.get("readonly", workspace_id == "root"))
            normalized_state[workspace_id] = payload
        if not normalized_state:
            raise ValueError(f"Checkpoint contains no workspace payload for session {session_id}.")
        return normalized_state

    def _import_session_context(
        self,
        session_id: str,
        *,
        source: str,
    ) -> tuple[RunSession, dict[str, AgentNode], WorkspaceManager, list[str], int, int]:
        self.latest_session_id = session_id
        checkpoint = self.storage.latest_checkpoint(session_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for session {session_id}")
        state = checkpoint["state"]
        session = self._session_from_state(state["session"])
        stored_session = self.storage.load_session(session_id)
        if stored_session is not None:
            session = self._session_from_storage_row(stored_session, fallback=session)
        remote_config = self._session_remote_config(session_id)
        if remote_config is not None and normalize_workspace_mode(session.workspace_mode) != WorkspaceMode.DIRECT:
            raise ValueError("Remote session config is only valid for direct workspace mode.")
        if remote_config is not None:
            self.project_dir = Path(remote_config.remote_dir).expanduser()
        else:
            self.project_dir = session.project_dir.resolve()
        self.tool_executor.set_project_dir(self.project_dir)
        self._apply_session_remote_runtime(
            session_id=session_id,
            remote_config=remote_config,
            require_password=False,
        )
        agents = {
            agent_id: self._agent_from_state(payload)
            for agent_id, payload in state["agents"].items()
        }
        self._ensure_unique_agent_names_in_session(session_id=session.id, agents=agents)
        root_agent = agents.get(session.root_agent_id)
        if root_agent is None:
            raise ValueError(
                f"Root agent {session.root_agent_id} is missing in checkpoint for session {session_id}."
            )
        self._restore_agent_conversations_from_messages(session_id, agents)
        normalized_workspaces = self._normalize_workspace_state_for_session(
            session_id=session_id,
            workspace_mode=session.workspace_mode,
            project_dir=self.project_dir,
            workspaces_state=state.get("workspaces"),
        )
        state["workspaces"] = normalized_workspaces
        workspace_manager = WorkspaceManager.from_state(
            self.paths.session_dir(session_id, create=False), normalized_workspaces
        )
        pending_agent_ids = [
            node.id
            for node in sorted(agents.values(), key=lambda item: item.id)
            if node.role == AgentRole.WORKER and self._is_active_agent(node)
        ]
        root_loop = max(
            int(session.loop_index),
            int(state.get("root_loop", 0) or 0),
        )
        session.loop_index = root_loop
        paused_agent_ids = self._pause_active_agents(
            session=session,
            agents=agents,
            reason=f"{source}_load",
        )
        cancelled_tool_run_ids = self._cancel_pending_tool_runs_for_agents_sync(
            session_id=session.id,
            agent_ids=set(paused_agent_ids),
            skip_tool_run_id=None,
            reason=(
                f"Cancelled because session context import ({source}) paused active agents."
            ),
        )
        if paused_agent_ids:
            pending_agent_ids = []
            if session.status == SessionStatus.RUNNING:
                self._set_session_status(
                    session,
                    SessionStatus.INTERRUPTED,
                    reason=f"context_import_{source}_paused_agents",
                )
        for agent in agents.values():
            self.storage.upsert_agent(agent)
        session.updated_at = utc_now()
        self.storage.upsert_session(session)
        checkpoint_seq = self._save_checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
            interrupted=session.status == SessionStatus.INTERRUPTED,
        )
        self._get_logger(session_id).log(
            session_id=session.id,
            agent_id=session.root_agent_id,
            parent_agent_id=None,
            event_type="session_context_imported",
            phase="runtime",
            payload={
                "source": source,
                "checkpoint_seq": checkpoint_seq,
                "previous_checkpoint_seq": checkpoint["seq"],
                "project_dir": str(self.project_dir),
                "pending_agent_ids": pending_agent_ids,
                "paused_agent_ids": paused_agent_ids,
                "cancelled_tool_run_ids": cancelled_tool_run_ids,
                "session_status": session.status.value,
            },
            workspace_id=root_agent.workspace_id,
        )
        self._log_diagnostic(
            "session_context_imported",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "source": source,
                "checkpoint_seq": checkpoint_seq,
                "previous_checkpoint_seq": checkpoint["seq"],
                "paused_agent_count": len(paused_agent_ids),
                "cancelled_tool_run_count": len(cancelled_tool_run_ids),
                "session_status": session.status.value,
            },
        )
        self._tool_run_waiters = {}
        self._active_tool_run_tasks = {}
        return (
            session,
            agents,
            workspace_manager,
            pending_agent_ids,
            root_loop,
            checkpoint_seq,
        )

    def _ensure_unique_agent_names_in_session(
        self,
        *,
        session_id: str,
        agents: dict[str, AgentNode],
    ) -> None:
        used: set[str] = set()
        for agent in sorted(agents.values(), key=lambda item: item.id):
            original = str(agent.name or "").strip() or "Agent"
            candidate = original
            suffix = 2
            while candidate.casefold() in used:
                candidate = f"{original} ({suffix})"
                suffix += 1
            if candidate != agent.name:
                previous = agent.name
                agent.name = candidate
                self.storage.upsert_agent(agent)
                self._log_diagnostic(
                    "agent_name_deduplicated",
                    session_id=session_id,
                    agent_id=agent.id,
                    payload={
                        "previous_name": previous,
                        "new_name": candidate,
                    },
                )
            used.add(candidate.casefold())

    def load_session_events(self, session_id: str) -> list[dict[str, Any]]:
        normalized_session_id = self._normalize_session_id(session_id)
        records: list[dict[str, Any]] = []
        for row in self.storage.load_events(normalized_session_id):
            payload = row.get("payload_json", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            records.append(
                {
                    "timestamp": str(row.get("timestamp", "")),
                    "session_id": str(row.get("session_id", normalized_session_id)),
                    "agent_id": row.get("agent_id"),
                    "parent_agent_id": row.get("parent_agent_id"),
                    "event_type": str(row.get("event_type", "")),
                    "phase": str(row.get("phase", "runtime")),
                    "payload": payload,
                    "workspace_id": row.get("workspace_id"),
                    "checkpoint_seq": int(row.get("checkpoint_seq", 0) or 0),
                }
            )
        return records

    def load_session_agents(self, session_id: str) -> list[dict[str, Any]]:
        normalized_session_id = self._normalize_session_id(session_id)
        agents: list[dict[str, Any]] = []
        for row in self.storage.load_agents(normalized_session_id):
            children: list[str] = []
            raw_children = row.get("children_json")
            if isinstance(raw_children, str):
                try:
                    parsed_children = json.loads(raw_children)
                except json.JSONDecodeError:
                    parsed_children = []
                if isinstance(parsed_children, list):
                    children = [
                        str(child_id).strip()
                        for child_id in parsed_children
                        if str(child_id).strip()
                    ]
            raw_parent_agent_id = row.get("parent_agent_id")
            parent_agent_id = (
                None
                if raw_parent_agent_id is None
                else (str(raw_parent_agent_id).strip() or None)
            )
            metadata: dict[str, Any] = {}
            raw_metadata = row.get("metadata_json")
            if isinstance(raw_metadata, str) and raw_metadata.strip():
                try:
                    parsed_metadata = json.loads(raw_metadata)
                except json.JSONDecodeError:
                    parsed_metadata = {}
                if isinstance(parsed_metadata, dict):
                    metadata = parsed_metadata
            model = str(metadata.get("model", "")).strip() or None
            context_latest_summary = str(metadata.get("context_summary", "") or "")
            summary_version = metadata.get("summary_version")
            summarized_until_message_index = metadata.get("summarized_until_message_index")
            current_context_tokens = metadata.get("current_context_tokens")
            context_limit_tokens = metadata.get("context_limit_tokens")
            usage_ratio = metadata.get("usage_ratio")
            compression_count = metadata.get("compression_count")
            last_compacted_message_range = metadata.get("last_compacted_message_range")
            last_compacted_step_range = metadata.get("last_compacted_step_range")
            last_usage_input_tokens = metadata.get("last_usage_input_tokens")
            last_usage_output_tokens = metadata.get("last_usage_output_tokens")
            last_usage_cache_read_tokens = metadata.get("last_usage_cache_read_tokens")
            last_usage_cache_write_tokens = metadata.get("last_usage_cache_write_tokens")
            last_usage_total_tokens = metadata.get("last_usage_total_tokens")
            try:
                normalized_context_tokens = (
                    max(0, int(current_context_tokens))
                    if current_context_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_context_tokens = None
            try:
                normalized_context_limit = (
                    max(0, int(context_limit_tokens))
                    if context_limit_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_context_limit = None
            try:
                normalized_usage_ratio = (
                    float(usage_ratio)
                    if usage_ratio is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_usage_ratio = None
            try:
                normalized_summary_version = (
                    max(0, int(summary_version))
                    if summary_version is not None
                    else 0
                )
            except (TypeError, ValueError):
                normalized_summary_version = 0
            try:
                normalized_summarized_until_message_index = (
                    int(summarized_until_message_index)
                    if summarized_until_message_index is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_summarized_until_message_index = None
            if (
                normalized_summarized_until_message_index is not None
                and normalized_summarized_until_message_index < -1
            ):
                normalized_summarized_until_message_index = -1
            try:
                normalized_compression_count = (
                    max(0, int(compression_count))
                    if compression_count is not None
                    else 0
                )
            except (TypeError, ValueError):
                normalized_compression_count = 0
            try:
                normalized_last_usage_input_tokens = (
                    max(0, int(last_usage_input_tokens))
                    if last_usage_input_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_last_usage_input_tokens = None
            try:
                normalized_last_usage_output_tokens = (
                    max(0, int(last_usage_output_tokens))
                    if last_usage_output_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_last_usage_output_tokens = None
            try:
                normalized_last_usage_cache_read_tokens = (
                    max(0, int(last_usage_cache_read_tokens))
                    if last_usage_cache_read_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_last_usage_cache_read_tokens = None
            try:
                normalized_last_usage_cache_write_tokens = (
                    max(0, int(last_usage_cache_write_tokens))
                    if last_usage_cache_write_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_last_usage_cache_write_tokens = None
            try:
                normalized_last_usage_total_tokens = (
                    max(0, int(last_usage_total_tokens))
                    if last_usage_total_tokens is not None
                    else None
                )
            except (TypeError, ValueError):
                normalized_last_usage_total_tokens = None
            agents.append(
                {
                    "id": str(row.get("id", "")),
                    "session_id": str(row.get("session_id", normalized_session_id)),
                    "name": str(row.get("name", "")),
                    "role": str(row.get("role", "")),
                    "instruction": str(row.get("instruction", "")),
                    "workspace_id": str(row.get("workspace_id", "")),
                    "parent_agent_id": parent_agent_id,
                    "status": str(row.get("status", "")),
                    "children": children,
                    "summary": str(row.get("summary", "") or ""),
                    "step_count": int(row.get("step_count", 0) or 0),
                    "model": model,
                    "keep_pinned_messages": max(
                        0, int(self.config.runtime.context.keep_pinned_messages)
                    ),
                    "context_latest_summary": context_latest_summary,
                    "summary_version": normalized_summary_version,
                    "summarized_until_message_index": normalized_summarized_until_message_index,
                    "current_context_tokens": normalized_context_tokens,
                    "context_limit_tokens": normalized_context_limit,
                    "usage_ratio": normalized_usage_ratio,
                    "compression_count": normalized_compression_count,
                    "last_usage_input_tokens": normalized_last_usage_input_tokens,
                    "last_usage_output_tokens": normalized_last_usage_output_tokens,
                    "last_usage_cache_read_tokens": normalized_last_usage_cache_read_tokens,
                    "last_usage_cache_write_tokens": normalized_last_usage_cache_write_tokens,
                    "last_usage_total_tokens": normalized_last_usage_total_tokens,
                    "last_compacted_message_range": (
                        last_compacted_message_range
                        if isinstance(last_compacted_message_range, dict)
                        else None
                    ),
                    "last_compacted_step_range": (
                        last_compacted_step_range
                        if isinstance(last_compacted_step_range, dict)
                        else None
                    ),
                }
            )
        return agents

    def list_session_messages(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
        tail: int | None = None,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        self._sync_session_messages_from_checkpoint(normalized_session_id)
        page = self._get_message_logger(normalized_session_id).list_records(
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
            tail=tail,
        )
        raw_messages = page.get("messages", [])
        if not isinstance(raw_messages, list) or not raw_messages:
            return page
        page["messages"] = self._annotate_prompt_visibility_for_records(
            normalized_session_id,
            raw_messages,
        )
        return page

    def _annotate_prompt_visibility_for_records(
        self,
        session_id: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        relevant_agent_ids = sorted(
            {
                str(record.get("agent_id", "")).strip()
                for record in records
                if isinstance(record, dict) and str(record.get("agent_id", "")).strip()
            }
        )
        projections = self._message_prompt_projections(
            session_id,
            agent_ids=relevant_agent_ids,
        )
        annotated: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            agent_id = str(record.get("agent_id", "")).strip()
            message_index = _safe_int(record.get("message_index"), default=-1)
            projection = projections.get(agent_id)
            if projection is None:
                bucket = "internal" if bool(record.get("internal", False)) else "tail"
            else:
                bucket = projection.bucket_for_message_index(message_index)
            prompt_visible = bucket in {"pinned", "tail"}
            payload = dict(record)
            payload["prompt_visible"] = prompt_visible
            payload["prompt_bucket"] = bucket
            annotated.append(payload)
        return annotated

    def _message_prompt_projections(
        self,
        session_id: str,
        *,
        agent_ids: list[str],
    ) -> dict[str, Any]:
        if not agent_ids:
            return {}
        keep_count = max(0, int(self.config.runtime.context.keep_pinned_messages))
        rows_by_agent = {
            str(row.get("id", "")).strip(): row
            for row in self.storage.load_agents(session_id)
            if isinstance(row, dict) and str(row.get("id", "")).strip()
        }
        logger = self._get_message_logger(session_id)
        projections: dict[str, Any] = {}
        for agent_id in agent_ids:
            full_records = logger.read(agent_id)
            if not full_records:
                continue
            agent_row = rows_by_agent.get(agent_id, {})
            metadata = self._message_projection_metadata(agent_row)
            internal_indices = {
                _safe_int(record.get("message_index"), default=-1)
                for record in full_records
                if isinstance(record, dict) and bool(record.get("internal", False))
            }
            internal_indices = {index for index in internal_indices if index >= 0}
            if internal_indices:
                merged_internal = {
                    _safe_int(value, default=-1)
                    for value in metadata.get("internal_message_indices", [])
                    if _safe_int(value, default=-1) >= 0
                }
                merged_internal.update(internal_indices)
                metadata = dict(metadata)
                metadata["internal_message_indices"] = sorted(merged_internal)
            conversation = self._conversation_from_message_records(full_records)
            if not conversation:
                continue
            role_text = str(agent_row.get("role", AgentRole.WORKER.value)).strip().lower()
            try:
                role = AgentRole(role_text)
            except ValueError:
                role = AgentRole.WORKER
            agent = AgentNode(
                id=agent_id,
                session_id=session_id,
                name=str(agent_row.get("name", agent_id) or agent_id),
                role=role,
                instruction=str(agent_row.get("instruction", "")),
                workspace_id=str(agent_row.get("workspace_id", "workspace")),
                metadata=metadata,
                conversation=conversation,
            )
            projections[agent_id] = prompt_window_projection(
                agent,
                keep_pinned_messages=keep_count,
            )
        return projections

    @staticmethod
    def _conversation_from_message_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conversation: list[dict[str, Any]] = []
        for record in sorted(
            records,
            key=lambda item: _safe_int(item.get("message_index"), default=-1),
        ):
            message_index = _safe_int(record.get("message_index"), default=-1)
            if message_index < 0:
                continue
            while len(conversation) <= message_index:
                conversation.append({"role": "assistant", "content": ""})
            message = record.get("message")
            if isinstance(message, dict):
                conversation[message_index] = message
            else:
                conversation[message_index] = {
                    "role": str(record.get("role", "")).strip() or "assistant",
                    "content": str(message or ""),
                }
        return conversation

    @staticmethod
    def _message_projection_metadata(agent_row: dict[str, Any]) -> dict[str, Any]:
        raw_metadata = agent_row.get("metadata_json")
        if not isinstance(raw_metadata, str) or not raw_metadata.strip():
            return {}
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def export_logs(self, session_id: str, export_path: Path | None = None) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        if export_path is None:
            resolved_export_path = (
                self.paths.existing_session_dir(normalized_session_id)
                / self.config.logging.export_filename
            )
        else:
            candidate_path = export_path.expanduser()
            if not candidate_path.is_absolute():
                candidate_path = (Path.cwd() / candidate_path).resolve()
            ensure_directory(candidate_path.parent)
            resolved_export_path = candidate_path
        self._sync_session_messages_from_checkpoint(normalized_session_id)
        tool_run_metrics = self.tool_run_metrics(normalized_session_id)
        steer_run_metrics = self.steer_run_metrics(normalized_session_id)
        resolved_export_path.write_text(
            stable_json_dumps(
                {
                    **self.storage.export_session(normalized_session_id),
                    "agent_messages": self._get_message_logger(normalized_session_id).read_all(),
                    "diagnostics": self.diagnostics.read(session_id=normalized_session_id),
                    "tool_run_metrics": tool_run_metrics,
                    "steer_run_metrics": steer_run_metrics,
                }
            ),
            encoding="utf-8",
        )
        self._log_diagnostic(
            "logs_exported",
            session_id=normalized_session_id,
            payload={"export_path": str(resolved_export_path)},
        )
        return resolved_export_path

    def export_tool_run_metrics(self, session_id: str, export_path: Path | None = None) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        if export_path is None:
            resolved_export_path = (
                self.paths.existing_session_dir(normalized_session_id) / "tool_run_metrics.json"
            )
        else:
            candidate_path = export_path.expanduser()
            if not candidate_path.is_absolute():
                candidate_path = (Path.cwd() / candidate_path).resolve()
            ensure_directory(candidate_path.parent)
            resolved_export_path = candidate_path
        metrics = self.tool_run_metrics(normalized_session_id)
        resolved_export_path.write_text(
            stable_json_dumps(metrics),
            encoding="utf-8",
        )
        self._log_diagnostic(
            "tool_run_metrics_exported",
            session_id=normalized_session_id,
            payload={"export_path": str(resolved_export_path)},
        )
        return resolved_export_path

    def list_tool_runs_page(
        self,
        session_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        default_limit, max_limit = self.config.runtime.tools.list_limit_bounds()
        bounded_limit = normalize_tool_run_limit(
            limit,
            default=default_limit,
            minimum=1,
            maximum=max_limit,
        )
        statuses, invalid_statuses = parse_tool_run_status_filters(status)
        if invalid_statuses:
            allowed = ", ".join(sorted(PENDING_TOOL_RUN_STATUSES | TERMINAL_TOOL_RUN_STATUSES))
            invalid = ", ".join(f"'{item}'" for item in invalid_statuses)
            raise ValueError(
                f"Invalid tool run status filter(s): {invalid}. Allowed: {allowed}."
            )
        decoded_cursor = decode_tool_run_cursor(cursor)
        if cursor is not None and decoded_cursor is None:
            raise ValueError("Invalid tool run cursor.")
        runs = self.storage.list_tool_runs(
            session_id=normalized_session_id,
            limit=bounded_limit + 1,
            statuses=statuses,
            cursor=decoded_cursor,
        )
        has_more = len(runs) > bounded_limit
        page_runs = runs[:bounded_limit]
        return {
            "tool_runs": page_runs,
            "next_cursor": (
                next_tool_run_cursor(page_runs, limit=bounded_limit) if has_more else None
            ),
            "has_more": has_more,
        }

    def list_tool_runs(
        self,
        session_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_session_id = self._normalize_session_id(session_id)
        return self.list_tool_runs_page(
            normalized_session_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )["tool_runs"]

    def tool_run_metrics(self, session_id: str) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        runs = self._list_tool_runs_all(session_id=normalized_session_id)
        return build_tool_run_metrics(runs, session_id=normalized_session_id)

    def list_steer_runs_page(
        self,
        session_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        default_limit, max_limit = self.config.runtime.tools.list_limit_bounds()
        bounded_limit = normalize_tool_run_limit(
            limit,
            default=default_limit,
            minimum=1,
            maximum=max_limit,
        )
        statuses, invalid_statuses = parse_steer_run_status_filters(status)
        if invalid_statuses:
            allowed = ", ".join(sorted(KNOWN_STEER_RUN_STATUSES))
            invalid = ", ".join(f"'{item}'" for item in invalid_statuses)
            raise ValueError(
                f"Invalid steer run status filter(s): {invalid}. Allowed: {allowed}."
            )
        decoded_cursor = decode_steer_run_cursor(cursor)
        if cursor is not None and decoded_cursor is None:
            raise ValueError("Invalid steer run cursor.")
        runs = self.storage.list_steer_runs(
            session_id=normalized_session_id,
            limit=bounded_limit + 1,
            statuses=statuses,
            cursor=decoded_cursor,
        )
        has_more = len(runs) > bounded_limit
        page_runs = runs[:bounded_limit]
        agent_names = self._agent_name_index_for_session(normalized_session_id)
        enriched_page_runs = [
            self._enrich_steer_run_record(
                record=run,
                agent_name_index=agent_names,
            )
            for run in page_runs
        ]
        return {
            "steer_runs": enriched_page_runs,
            "next_cursor": (
                next_steer_run_cursor(page_runs, limit=bounded_limit) if has_more else None
            ),
            "has_more": has_more,
        }

    def _agent_name_index_for_session(self, session_id: str) -> dict[str, str]:
        index: dict[str, str] = {}
        for row in self.storage.load_agents(session_id):
            agent_id = str(row.get("id", "")).strip()
            if not agent_id:
                continue
            agent_name = str(row.get("name", "")).strip()
            index[agent_id] = agent_name or agent_id
        return index

    def _enrich_steer_run_record(
        self,
        *,
        record: dict[str, Any],
        agent_name_index: dict[str, str],
    ) -> dict[str, Any]:
        enriched = dict(record)

        target_agent_id = str(enriched.get("agent_id", "")).strip()
        target_agent_name = str(
            enriched.get("target_agent_name", enriched.get("agent_name", "")) or ""
        ).strip()
        if not target_agent_name and target_agent_id:
            target_agent_name = str(agent_name_index.get(target_agent_id, "")).strip()
        if target_agent_id and not target_agent_name:
            target_agent_name = target_agent_id
        enriched["target_agent_name"] = target_agent_name

        source_agent_id = str(enriched.get("source_agent_id", "")).strip() or USER_STEER_SOURCE_ID
        source_agent_name = str(enriched.get("source_agent_name", "")).strip()
        if source_agent_id == USER_STEER_SOURCE_ID:
            source_agent_name = source_agent_name or self._steer_user_label()
        else:
            source_agent_name = source_agent_name or str(
                agent_name_index.get(source_agent_id, "")
            ).strip()
            if not source_agent_name:
                source_agent_name = source_agent_id
        enriched["source_agent_id"] = source_agent_id
        enriched["source_agent_name"] = source_agent_name
        return enriched

    def steer_run_metrics(self, session_id: str) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        runs = self._list_steer_runs_all(session_id=normalized_session_id)
        return build_steer_run_metrics(runs, session_id=normalized_session_id)

    def submit_steer_run(
        self,
        *,
        session_id: str,
        agent_id: str,
        content: str,
        source: str = "manual",
        source_agent_id: str = USER_STEER_SOURCE_ID,
        source_agent_name: str | None = None,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_agent_id = str(agent_id or "").strip()
        normalized_content = str(content or "").strip()
        normalized_source = str(source or "").strip() or "manual"
        if not normalized_agent_id:
            raise ValueError("agent_id is required.")
        if not normalized_content:
            raise ValueError("steer content is required.")
        session_row = self.storage.load_session(normalized_session_id)
        if session_row is None:
            raise ValueError(f"Session {normalized_session_id} was not found.")
        agent_row = self._agent_row_for_session(normalized_session_id, normalized_agent_id)
        if agent_row is None:
            raise ValueError(
                f"Agent {normalized_agent_id} was not found in session {normalized_session_id}."
            )
        resolved_source_agent_id, resolved_source_agent_name = self._normalize_steer_source_actor(
            session_id=normalized_session_id,
            source_agent_id=source_agent_id,
            source_agent_name=source_agent_name,
        )
        if (
            resolved_source_agent_id != USER_STEER_SOURCE_ID
            and resolved_source_agent_id == normalized_agent_id
        ):
            raise ValueError("steer cannot target the source agent itself.")
        normalized_content = self._compose_steer_content_for_target(
            content=normalized_content,
            agent_row=agent_row,
            source_agent_id=resolved_source_agent_id,
            source_agent_name=resolved_source_agent_name,
        )
        run = SteerRun(
            id=self._new_steer_run_id(),
            session_id=normalized_session_id,
            agent_id=normalized_agent_id,
            content=normalized_content,
            source=normalized_source,
            status=SteerRunStatus.WAITING,
            source_agent_id=resolved_source_agent_id,
            source_agent_name=resolved_source_agent_name,
            created_at=utc_now(),
        )
        self.storage.upsert_steer_run(run)
        agent_row = self._reactivate_agent_for_steer_if_needed(
            session_id=normalized_session_id,
            session_row=session_row,
            agent_id=normalized_agent_id,
            agent_row=agent_row,
        )
        record = self.storage.load_steer_run(run.id)
        if record is None:
            record = {
                "id": run.id,
                "session_id": run.session_id,
                "agent_id": run.agent_id,
                "content": run.content,
                "source": run.source,
                "source_agent_id": run.source_agent_id,
                "source_agent_name": run.source_agent_name,
                "status": run.status.value,
                "created_at": run.created_at,
                "completed_at": None,
                "cancelled_at": None,
                "delivered_step": None,
            }
        enriched_record = self._enrich_steer_run_record(
            record=record,
            agent_name_index=self._agent_name_index_for_session(normalized_session_id),
        )
        self._log_steer_run_event(
            session_id=normalized_session_id,
            agent_row=agent_row,
            event_type="steer_run_submitted",
            payload={
                "steer_run_id": run.id,
                "status": run.status.value,
                "source": run.source,
                "source_agent_id": run.source_agent_id,
                "source_agent_name": run.source_agent_name,
                "content": run.content,
            },
        )
        return enriched_record

    def _normalize_steer_source_actor(
        self,
        *,
        session_id: str,
        source_agent_id: str,
        source_agent_name: str | None,
    ) -> tuple[str, str]:
        normalized_source_agent_id = str(source_agent_id or "").strip() or USER_STEER_SOURCE_ID
        normalized_source_agent_name = str(source_agent_name or "").strip()
        if normalized_source_agent_id == USER_STEER_SOURCE_ID:
            return USER_STEER_SOURCE_ID, normalized_source_agent_name or self._steer_user_label()
        agent_row = self._agent_row_for_session(session_id, normalized_source_agent_id)
        if agent_row is None:
            raise ValueError(
                f"Steer source agent {normalized_source_agent_id} was not found in session {session_id}."
            )
        resolved_name = normalized_source_agent_name or str(agent_row.get("name", "")).strip()
        return normalized_source_agent_id, resolved_name or normalized_source_agent_id

    def _steer_user_label(self) -> str:
        return "用户" if self.locale == "zh" else "user"

    def _steer_signature_line(self, source_agent_id: str, source_agent_name: str) -> str:
        prefix = "来自于" if self.locale == "zh" else "from"
        if str(source_agent_id).strip() == USER_STEER_SOURCE_ID:
            return f"--- {prefix} {self._steer_user_label()}"
        return f"--- {prefix} {source_agent_name} ({source_agent_id})"

    def _compose_steer_content_for_target(
        self,
        *,
        content: str,
        agent_row: dict[str, Any],
        source_agent_id: str,
        source_agent_name: str,
    ) -> str:
        normalized_content = str(content or "").strip()
        intro = self._runtime_message("steer_message_intro").strip()
        if intro and not normalized_content.startswith(intro):
            normalized_content = f"{intro}\n\n{normalized_content}"
        signature = self._steer_signature_line(source_agent_id, source_agent_name).strip()
        if signature and not normalized_content.endswith(signature):
            normalized_content = f"{normalized_content}\n\n{signature}"
        status = str(agent_row.get("status", "")).strip().lower()
        if status != AgentStatus.COMPLETED.value:
            return normalized_content
        reminder = self._runtime_message("steer_completed_agent_finish_reminder").strip()
        if not reminder:
            return normalized_content
        if normalized_content.endswith(reminder):
            return normalized_content
        return f"{normalized_content}\n\n{reminder}"

    def _reactivate_agent_for_steer_if_needed(
        self,
        *,
        session_id: str,
        session_row: dict[str, Any],
        agent_id: str,
        agent_row: dict[str, Any],
    ) -> dict[str, Any]:
        session_status = str(session_row.get("status", "")).strip().lower()
        if session_status != SessionStatus.RUNNING.value:
            return agent_row
        raw_status = str(agent_row.get("status", "")).strip().lower()
        try:
            status = AgentStatus(raw_status)
        except ValueError:
            return agent_row
        if status not in NON_SCHEDULABLE_AGENT_STATUSES:
            return agent_row

        runtime_context = self._live_session_contexts.get(session_id)
        if runtime_context is None:
            return agent_row
        live_session, live_agents, workspace_manager = runtime_context
        live_agent = live_agents.get(agent_id)
        if live_agent is None:
            return agent_row
        if live_agent.status not in NON_SCHEDULABLE_AGENT_STATUSES:
            refreshed = self._agent_row_for_session(session_id, agent_id)
            return refreshed or agent_row

        previous_status = live_agent.status
        previous_completion_status = live_agent.completion_status
        self._set_agent_status(
            live_agent,
            AgentStatus.RUNNING,
            reason="reactivated_by_steer",
            explicit_reopen=True,
        )
        live_agent.completion_status = None
        self.storage.upsert_agent(live_agent)
        if (
            live_session.status == SessionStatus.RUNNING
        ):
            if live_agent.role == AgentRole.ROOT:
                self._ensure_root_tasks_started(
                    session=live_session,
                    agents=live_agents,
                    workspace_manager=workspace_manager,
                    agent_ids=[live_agent.id],
                )
            else:
                self._ensure_worker_tasks_started(
                    session=live_session,
                    agents=live_agents,
                    workspace_manager=workspace_manager,
                    root_loop=max(0, int(live_session.loop_index)),
                    agent_ids=[live_agent.id],
                )
            self._signal_runtime_change()
        self._log_diagnostic(
            "steer_reactivated_agent",
            session_id=session_id,
            agent_id=live_agent.id,
            payload={
                "previous_status": previous_status.value,
                "previous_completion_status": previous_completion_status,
                "new_status": live_agent.status.value,
            },
        )
        refreshed = self._agent_row_for_session(session_id, agent_id)
        return refreshed or agent_row

    def _reactivate_agent_in_resume_context_if_needed(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        agent_id: str | None,
    ) -> None:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return
        agent = agents.get(normalized_agent_id)
        if agent is None:
            return
        if agent.status not in NON_SCHEDULABLE_AGENT_STATUSES:
            return
        previous_status = agent.status
        previous_completion_status = agent.completion_status
        self._set_agent_status(
            agent,
            AgentStatus.RUNNING,
            reason="reactivated_by_resume",
            explicit_reopen=True,
        )
        agent.completion_status = None
        self.storage.upsert_agent(agent)
        self._log_diagnostic(
            "resume_reactivated_agent",
            session_id=session.id,
            agent_id=agent.id,
            payload={
                "previous_status": previous_status.value,
                "previous_completion_status": previous_completion_status,
                "new_status": agent.status.value,
            },
        )

    async def terminate_agent_subtree(
        self,
        *,
        session_id: str,
        agent_id: str,
        source: str = "ui",
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            raise ValueError("agent_id is required.")
        runtime_context = self._live_session_contexts.get(normalized_session_id)
        if runtime_context is None:
            raise RuntimeError(
                f"Session {normalized_session_id} is not active in the current runtime."
            )
        session, agents, _workspace_manager = runtime_context
        target = agents.get(normalized_agent_id)
        if target is None:
            raise ValueError(
                f"Agent {normalized_agent_id} was not found in session {normalized_session_id}."
            )

        target_ids = self._cancel_agent_target_ids(
            target_agent_id=normalized_agent_id,
            recursive=True,
            agents=agents,
        )
        target_id_set = set(target_ids)
        normalized_source = str(source or "ui").strip() or "ui"
        reason = (
            f"Cancelled by user via {normalized_source} for subtree "
            f"{normalized_agent_id}."
        )
        cancelled_agent_ids: list[str] = []
        for target_id in target_ids:
            node = agents.get(target_id)
            if node is None:
                continue
            if node.status in {
                AgentStatus.COMPLETED,
                AgentStatus.FAILED,
                AgentStatus.TERMINATED,
            }:
                continue
            previous_status = node.status
            self._set_agent_status(
                node,
                AgentStatus.CANCELLED,
                reason=f"cancel_source:{normalized_source}",
            )
            node.completion_status = "cancelled"
            if not str(node.summary or "").strip():
                node.summary = reason
            self.storage.upsert_agent(node)
            cancelled_agent_ids.append(node.id)
            self._log_agent_event(
                node,
                event_type="agent_cancelled",
                phase="runtime",
                payload={
                    "reason": reason,
                    "previous_status": previous_status.value,
                    "cancel_source": normalized_source,
                    "source": normalized_source,
                    "target_agent_id": normalized_agent_id,
                },
            )

        await self._cancel_worker_tasks_for_agents(target_id_set)
        cancelled_tool_run_ids = await self._cancel_pending_tool_runs_for_agents(
            session_id=normalized_session_id,
            agent_ids=target_id_set,
            skip_tool_run_id=None,
            reason=(
                "Cancelled because owner/child agent was cancelled by user action "
                f"({normalized_source})."
            ),
        )
        if cancelled_agent_ids or cancelled_tool_run_ids:
            session.updated_at = utc_now()
            self.storage.upsert_session(session)
        self._log_diagnostic(
            "agent_subtree_cancelled_by_user",
            level="warning",
            session_id=normalized_session_id,
            agent_id=normalized_agent_id,
            payload={
                "source": normalized_source,
                "target_agent_ids": target_ids,
                "cancelled_agent_ids": cancelled_agent_ids,
                "cancelled_tool_run_ids": cancelled_tool_run_ids,
            },
        )
        return {
            "session_id": normalized_session_id,
            "agent_id": normalized_agent_id,
            "source": normalized_source,
            "target_agent_ids": target_ids,
            "cancelled_agent_ids": cancelled_agent_ids,
            "cancelled_tool_run_ids": cancelled_tool_run_ids,
        }

    def cancel_steer_run(
        self,
        *,
        session_id: str,
        steer_run_id: str,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_run_id = str(steer_run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("steer_run_id is required.")
        record = self.storage.load_steer_run(normalized_run_id)
        if record is None:
            raise ValueError(f"Steer run {normalized_run_id} was not found.")
        if str(record.get("session_id", "")).strip() != normalized_session_id:
            raise ValueError(f"Steer run {normalized_run_id} is outside the current session.")
        status = str(record.get("status", "")).strip().lower()
        agent_row = self._agent_row_for_session(
            normalized_session_id,
            str(record.get("agent_id", "")).strip(),
        )
        if status in TERMINAL_STEER_RUN_STATUSES:
            enriched_record = self._enrich_steer_run_record(
                record=record,
                agent_name_index=self._agent_name_index_for_session(normalized_session_id),
            )
            return {
                "steer_run_id": normalized_run_id,
                "final_status": status or "unknown",
                "cancelled": False,
                "steer_run": enriched_record,
            }
        transitioned = self.storage.cancel_waiting_steer_run(
            session_id=normalized_session_id,
            steer_run_id=normalized_run_id,
            cancelled_at=utc_now(),
        )
        final_record = transitioned or self.storage.load_steer_run(normalized_run_id) or record
        final_status = str(final_record.get("status", "")).strip().lower() or "unknown"
        if transitioned is not None:
            self._log_steer_run_event(
                session_id=normalized_session_id,
                agent_row=agent_row,
                event_type="steer_run_updated",
                payload={
                    "steer_run_id": normalized_run_id,
                    "status": final_status,
                    "source": str(final_record.get("source", "")).strip(),
                    "source_agent_id": str(final_record.get("source_agent_id", "")).strip(),
                    "source_agent_name": str(final_record.get("source_agent_name", "")).strip(),
                    "cancelled_at": final_record.get("cancelled_at"),
                },
            )
        enriched_final_record = self._enrich_steer_run_record(
            record=final_record,
            agent_name_index=self._agent_name_index_for_session(normalized_session_id),
        )
        return {
            "steer_run_id": normalized_run_id,
            "final_status": final_status,
            "cancelled": final_status == SteerRunStatus.CANCELLED.value,
            "steer_run": enriched_final_record,
        }

    def resolve_session_workspace_path(self, session_id: str) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        session_dir = self.paths.existing_session_dir(normalized_session_id).resolve()
        if not session_dir.exists() or not session_dir.is_dir():
            raise ValueError(f"Session folder does not exist: {normalized_session_id}")
        remote_config = self._session_remote_config(normalized_session_id)
        if remote_config is not None:
            return Path(remote_config.remote_dir).expanduser()

        checkpoint = self.storage.latest_checkpoint(normalized_session_id)
        if checkpoint:
            checkpoint_state = checkpoint.get("state")
            if isinstance(checkpoint_state, dict):
                session_payload = checkpoint_state.get("session")
                if isinstance(session_payload, dict):
                    session = self._session_from_state(session_payload)
                else:
                    session = None
                workspaces_state = checkpoint_state.get("workspaces")
                if isinstance(workspaces_state, dict) and session is not None:
                    normalized_workspaces = self._normalize_workspace_state_for_session(
                        session_id=normalized_session_id,
                        workspace_mode=session.workspace_mode,
                        project_dir=session.project_dir,
                        workspaces_state=workspaces_state,
                    )
                    root_workspace = normalized_workspaces.get("root")
                    if isinstance(root_workspace, dict):
                        root_path_text = str(root_workspace.get("path", "")).strip()
                        if root_path_text:
                            root_path = Path(root_path_text).expanduser().resolve()
                            if root_path.exists() and root_path.is_dir():
                                return root_path

        inferred_paths = [
            (session_dir / "snapshots" / "root").resolve(),
            (session_dir / "workspaces" / "root").resolve(),
        ]
        for candidate in inferred_paths:
            if candidate.exists() and candidate.is_dir():
                return candidate
        raise ValueError(
            f"Could not resolve root workspace for session {normalized_session_id}."
        )

    def _build_session_terminal_request(
        self,
        session_id: str,
        *,
        remote_password: str | None = None,
        require_password: bool = True,
    ) -> tuple[str, Path, ShellCommandRequest]:
        normalized_session_id = self._normalize_session_id(session_id)
        remote_config = self._session_remote_config(normalized_session_id)
        self._apply_session_remote_runtime(
            session_id=normalized_session_id,
            remote_config=remote_config,
            remote_password=remote_password,
            require_password=require_password,
        )
        workspace_root = self.resolve_session_workspace_path(normalized_session_id)
        request = self.tool_executor.build_shell_request(
            workspace_root=workspace_root,
            command=":",
            cwd=".",
            writable_paths=[workspace_root],
            environment={},
            session_id=normalized_session_id,
            remote=self.tool_executor.session_remote_context(normalized_session_id),
        )
        return normalized_session_id, workspace_root, request

    def _shell_backend(self) -> SandboxBackend:
        return self.tool_executor.shell_backend()

    @staticmethod
    def _remote_terminal_control_path(session_id: str, ssh_target: str) -> Path:
        if os.name == "nt":
            base = ensure_directory(Path(tempfile.gettempdir()) / "opencompany_ssh")
        else:
            base = ensure_directory(Path("/tmp/opencompany-ssh"))
        digest = hashlib.sha256(f"{session_id}::{ssh_target}".encode("utf-8")).hexdigest()[:16]
        return base / f"tm-{digest}"

    def open_session_terminal(
        self,
        session_id: str,
        *,
        remote_password: str | None = None,
    ) -> dict[str, Any]:
        normalized_session_id, workspace_root, request = self._build_session_terminal_request(
            session_id,
            remote_password=remote_password,
            require_password=False,
        )
        backend = self._shell_backend()
        session_dir = self.paths.existing_session_dir(normalized_session_id)
        terminal_dir = ensure_directory(session_dir / "terminal")
        for legacy in terminal_dir.glob("settings_*.json"):
            legacy.unlink(missing_ok=True)
        settings_path = terminal_dir / "settings.json"
        settings_payload = backend.build_settings(request)
        settings_path.write_text(
            json.dumps(settings_payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        terminal_password_file: Path | None = None
        remote_context = request.remote
        if remote_context is not None:
            remote_user, remote_host, remote_port = parse_ssh_target(remote_context.config.ssh_target)
            remote_ssh_destination = f"{remote_user}@{remote_host}"
            settings_blob = base64.b64encode(
                json.dumps(settings_payload, ensure_ascii=True).encode("utf-8")
            ).decode("ascii")
            settings_hash = hashlib.sha256(
                json.dumps(settings_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            remote_cache_dir = f"${{HOME}}/.opencompany_remote/{normalized_session_id}"
            remote_settings_path = f"{remote_cache_dir}/settings.json"
            remote_hash_path = f"{remote_cache_dir}/settings.sha256"
            build_terminal_command = getattr(backend, "build_terminal_command", None)
            remote_command: str | None = None
            if callable(build_terminal_command):
                maybe_remote_command = build_terminal_command(
                    request,
                    settings_path=settings_path,
                    remote_settings_path=remote_settings_path,
                )
                if isinstance(maybe_remote_command, str) and maybe_remote_command.strip():
                    remote_command = maybe_remote_command.strip()
            if remote_command is None:
                dependency_setup = AnthropicSandboxBackend.remote_dependency_guard_script(
                    normalized_session_id
                )
                remote_command = (
                    f"{dependency_setup}; "
                    f"workspace_root={shlex.quote(str(workspace_root))}; "
                    f"mkdir -p {remote_cache_dir}; "
                    f"current=''; if [ -f {remote_hash_path} ]; then current=$(cat {remote_hash_path} 2>/dev/null || true); fi; "
                    f"if [ \"$current\" != {shlex.quote(settings_hash)} ]; then "
                    f"printf %s {shlex.quote(settings_blob)} | base64 --decode > {remote_settings_path}.tmp && "
                    f"mv {remote_settings_path}.tmp {remote_settings_path} && "
                    f"printf %s {shlex.quote(settings_hash)} > {remote_hash_path}; "
                    "fi; "
                    "resolved_workspace_root=\"$workspace_root\"; "
                    "if [ -d \"$workspace_root\" ]; then resolved_workspace_root=$(cd \"$workspace_root\" && /bin/pwd -P); fi; "
                    "cd \"$resolved_workspace_root\"; "
                    f"exec srt --settings {remote_settings_path} /bin/bash --noprofile --norc -i"
                )
            control_path = self._remote_terminal_control_path(
                normalized_session_id,
                remote_context.config.ssh_target,
            )
            ssh_options = [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=600",
                "-o",
                f"ControlPath={str(control_path)}",
                "-o",
                (
                    "StrictHostKeyChecking=yes"
                    if remote_context.config.known_hosts_policy == "strict"
                    else "StrictHostKeyChecking=accept-new"
                ),
                "-t",
            ]
            if remote_port is not None:
                ssh_options.extend(["-p", str(remote_port)])
            if remote_context.config.auth_mode == "key":
                ssh_command = shlex.join(
                    [
                        "ssh",
                        *ssh_options,
                        "-i",
                        str(Path(remote_context.config.identity_file).expanduser()),
                        remote_ssh_destination,
                        remote_command,
                    ]
                )
            else:
                remote_password_text = str(remote_context.password or "").strip()
                sshpass_binary = shutil.which("sshpass")
                if remote_password_text and sshpass_binary:
                    terminal_password_file = terminal_dir / f"sshpass_{uuid.uuid4().hex}.txt"
                    terminal_password_file.write_text(remote_password_text, encoding="utf-8")
                    with suppress(Exception):
                        os.chmod(terminal_password_file, 0o600)
                    ssh_command = shlex.join(
                        [
                            sshpass_binary,
                            "-f",
                            str(terminal_password_file),
                            "ssh",
                            *ssh_options,
                            remote_ssh_destination,
                            remote_command,
                        ]
                    )
                    script_command = (
                        f"cd {shlex.quote(str(self.app_dir))} "
                        f"&& password_file={shlex.quote(str(terminal_password_file))}; "
                        "cleanup(){ rm -f \"$password_file\"; }; "
                        "trap cleanup EXIT INT TERM; "
                        f"{ssh_command} "
                        "|| { status=$?; echo 'Failed to launch remote sandbox terminal.'; exit \"$status\"; }"
                    )
                else:
                    # Fallback: interactive SSH prompt when password is unavailable
                    # or sshpass is not installed locally.
                    ssh_command = shlex.join(
                        [
                            "ssh",
                            *ssh_options,
                            remote_ssh_destination,
                            remote_command,
                        ]
                    )
                    script_command = (
                        f"cd {shlex.quote(str(self.app_dir))} "
                        f"&& exec {ssh_command} "
                        f"|| {{ echo 'Failed to launch remote sandbox terminal.'; exit 1; }}"
                    )
            if remote_context.config.auth_mode == "key":
                script_command = (
                    f"cd {shlex.quote(str(self.app_dir))} "
                    f"&& exec {ssh_command} "
                    f"|| {{ echo 'Failed to launch remote sandbox terminal.'; exit 1; }}"
                )
        else:
            build_terminal_command = getattr(backend, "build_terminal_command", None)
            runtime_command: str | None = None
            if callable(build_terminal_command):
                maybe_runtime_command = build_terminal_command(
                    request,
                    settings_path=settings_path,
                )
                if isinstance(maybe_runtime_command, str) and maybe_runtime_command.strip():
                    runtime_command = maybe_runtime_command.strip()
            if runtime_command is None:
                runtime_command = shlex.join(
                    [
                        backend.resolve_cli_path(),
                        "--settings",
                        str(settings_path),
                        "/bin/bash",
                        "--noprofile",
                        "--norc",
                        "-i",
                    ]
                )
            script_command = (
                f"cd {shlex.quote(str(workspace_root))} "
                f"&& exec {runtime_command} "
                f"|| {{ echo 'Failed to launch sandbox terminal.'; exit 1; }}"
            )
        try:
            self._open_system_terminal(script_command)
        except Exception:
            if terminal_password_file is not None:
                terminal_password_file.unlink(missing_ok=True)
            raise

        return {
            "session_id": normalized_session_id,
            "workspace_root": str(workspace_root),
            "settings_path": str(settings_path),
        }

    async def terminal_self_check(
        self,
        session_id: str,
        *,
        remote_password: str | None = None,
    ) -> dict[str, Any]:
        normalized_session_id, workspace_root, terminal_request = self._build_session_terminal_request(
            session_id,
            remote_password=remote_password,
            require_password=True,
        )
        backend = self._shell_backend()
        session_dir = self.paths.existing_session_dir(normalized_session_id).resolve()
        terminal_dir = ensure_directory(session_dir / "terminal")

        probe_request = self.tool_executor.build_shell_request(
            workspace_root=workspace_root,
            command="echo terminal-self-check",
            cwd=".",
            writable_paths=[workspace_root],
            environment={},
            session_id=normalized_session_id,
            remote=self.tool_executor.session_remote_context(normalized_session_id),
        )
        policy_match = (
            terminal_request.cwd == probe_request.cwd
            and terminal_request.workspace_root == probe_request.workspace_root
            and terminal_request.writable_paths == probe_request.writable_paths
            and terminal_request.timeout_seconds == probe_request.timeout_seconds
            and terminal_request.network_policy == probe_request.network_policy
            and terminal_request.allowed_domains == probe_request.allowed_domains
        )
        terminal_settings = backend.build_settings(terminal_request)
        agent_settings = backend.build_settings(probe_request)
        settings_match = terminal_settings == agent_settings

        marker = uuid.uuid4().hex[:8]
        inside_name = f".terminal_self_check_{marker}.tmp"
        inside_path = workspace_root / inside_name
        outside_path = terminal_dir / f"terminal_escape_{marker}.tmp"
        outside_path.unlink(missing_ok=True)

        inside_request = self.tool_executor.build_shell_request(
            workspace_root=workspace_root,
            command=(
                f"printf 'ok' > {shlex.quote(inside_name)} "
                f"&& test -f {shlex.quote(inside_name)} "
                f"&& rm -f {shlex.quote(inside_name)}"
            ),
            cwd=".",
            writable_paths=[workspace_root],
            environment={},
            session_id=normalized_session_id,
            remote=self.tool_executor.session_remote_context(normalized_session_id),
        )
        outside_request = self.tool_executor.build_shell_request(
            workspace_root=workspace_root,
            command=f"printf 'blocked' > {shlex.quote(str(outside_path))}",
            cwd=".",
            writable_paths=[workspace_root],
            environment={},
            session_id=normalized_session_id,
            remote=self.tool_executor.session_remote_context(normalized_session_id),
        )

        runtime_error: str | None = None
        inside_result: dict[str, Any] | None = None
        outside_result: dict[str, Any] | None = None
        outside_exists_after_run = False
        workspace_write_ok = False
        outside_write_blocked = False
        expected_outside_write_blocked = True
        should_block = getattr(backend, "should_block_outside_workspace_write", None)
        if callable(should_block):
            with suppress(Exception):
                expected_outside_write_blocked = bool(should_block())
        outside_write_policy_match = False
        try:
            inside_run = await backend.run_command(inside_request)
            outside_run = await backend.run_command(outside_request)
            inside_result = {
                "exit_code": inside_run.exit_code,
                "stderr_preview": truncate_text(inside_run.stderr, 300),
                "stdout_preview": truncate_text(inside_run.stdout, 120),
            }
            outside_result = {
                "exit_code": outside_run.exit_code,
                "stderr_preview": truncate_text(outside_run.stderr, 300),
                "stdout_preview": truncate_text(outside_run.stdout, 120),
            }
            outside_exists_after_run = outside_path.exists()
            workspace_write_ok = inside_run.exit_code == 0 and not inside_path.exists()
            if not workspace_write_ok:
                runtime_error = truncate_text(
                    inside_run.stderr
                    or inside_run.stdout
                    or f"workspace_write_failed(exit_code={inside_run.exit_code})",
                    300,
                )
            if workspace_write_ok:
                outside_write_blocked = outside_run.exit_code != 0 and not outside_exists_after_run
                outside_write_policy_match = (
                    outside_write_blocked == expected_outside_write_blocked
                )
        except Exception as exc:
            runtime_error = str(exc)
        finally:
            inside_path.unlink(missing_ok=True)
            outside_path.unlink(missing_ok=True)

        checks = {
            "policy_match_agent_shell": {"ok": policy_match},
            "settings_match_agent_shell": {"ok": settings_match},
            "workspace_write": {
                "ok": workspace_write_ok,
                "path": str(inside_path),
                "result": inside_result,
            },
            "outside_write_blocked": {
                "ok": outside_write_blocked,
                "path": str(outside_path),
                "exists_after": outside_exists_after_run,
                "result": outside_result,
            },
            "outside_write_policy_match": {
                "ok": outside_write_policy_match,
                "expected_blocked": expected_outside_write_blocked,
                "observed_blocked": outside_write_blocked,
            },
        }
        passed = bool(
            policy_match
            and settings_match
            and workspace_write_ok
            and outside_write_policy_match
            and runtime_error is None
        )
        return {
            "session_id": normalized_session_id,
            "workspace_root": str(workspace_root),
            "passed": passed,
            "runtime_error": runtime_error,
            "outside_write_expected_blocked": expected_outside_write_blocked,
            "checks": checks,
        }

    def _open_system_terminal(self, command: str) -> None:
        normalized = str(command or "").strip()
        if not normalized:
            raise ValueError("Terminal launch command is empty.")
        if sys.platform == "darwin":
            script_dir = ensure_directory(self.paths.data_dir / "terminal_launch")
            for legacy in script_dir.glob("launch_*.command"):
                if legacy.name.count("_") >= 2:
                    legacy.unlink(missing_ok=True)
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            script_path = script_dir / f"launch_{digest}.command"
            script_path.write_text(
                "#!/bin/bash\n"
                "set -e\n"
                f"{normalized}\n",
                encoding="utf-8",
            )
            script_path.chmod(0o700)
            completed = subprocess.run(
                [
                    "open",
                    "-a",
                    "Terminal",
                    str(script_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                error = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(error or "Failed to open Terminal.app.")
            return

        if sys.platform.startswith("linux"):
            candidates: list[list[str]] = []
            if shutil.which("gnome-terminal"):
                candidates.append(["gnome-terminal", "--", "bash", "-lc", normalized])
            if shutil.which("x-terminal-emulator"):
                candidates.append(["x-terminal-emulator", "-e", "bash", "-lc", normalized])
            if shutil.which("konsole"):
                candidates.append(["konsole", "-e", "bash", "-lc", normalized])
            if shutil.which("xfce4-terminal"):
                candidates.append(["xfce4-terminal", "-e", f"bash -lc {shlex.quote(normalized)}"])
            if shutil.which("xterm"):
                candidates.append(["xterm", "-e", "bash", "-lc", normalized])
            if shutil.which("alacritty"):
                candidates.append(["alacritty", "-e", "bash", "-lc", normalized])
            for candidate in candidates:
                try:
                    subprocess.Popen(candidate)
                    return
                except OSError:
                    continue
            raise RuntimeError("No supported system terminal was found on this Linux host.")

        raise RuntimeError(f"Unsupported platform for system terminal launch: {sys.platform}")

    def _list_tool_runs_all(
        self,
        *,
        session_id: str,
        statuses: str | list[str] | None = None,
        agent_id: str | None = None,
        batch_size: int = 500,
    ) -> list[dict[str, Any]]:
        bounded_batch = normalize_tool_run_limit(
            batch_size,
            default=500,
            minimum=1,
            maximum=1_000,
        )
        normalized_statuses, invalid_statuses = parse_tool_run_status_filters(statuses)
        if invalid_statuses:
            invalid = ", ".join(f"'{item}'" for item in invalid_statuses)
            raise ValueError(f"Invalid tool run status filter(s): {invalid}.")
        records: list[dict[str, Any]] = []
        cursor: tuple[str, str] | None = None
        while True:
            batch = self.storage.list_tool_runs(
                session_id=session_id,
                agent_id=agent_id,
                statuses=normalized_statuses,
                cursor=cursor,
                limit=bounded_batch,
            )
            if not batch:
                break
            records.extend(batch)
            cursor_token = next_tool_run_cursor(batch, limit=bounded_batch)
            if not cursor_token:
                break
            cursor = decode_tool_run_cursor(cursor_token)
            if cursor is None:
                break
        return records

    def _list_steer_runs_all(
        self,
        *,
        session_id: str,
        statuses: str | list[str] | None = None,
        agent_id: str | None = None,
        batch_size: int = 500,
    ) -> list[dict[str, Any]]:
        bounded_batch = normalize_tool_run_limit(
            batch_size,
            default=500,
            minimum=1,
            maximum=1_000,
        )
        normalized_statuses, invalid_statuses = parse_steer_run_status_filters(statuses)
        if invalid_statuses:
            invalid = ", ".join(f"'{item}'" for item in invalid_statuses)
            raise ValueError(f"Invalid steer run status filter(s): {invalid}.")
        records: list[dict[str, Any]] = []
        cursor: tuple[str, str] | None = None
        while True:
            batch = self.storage.list_steer_runs(
                session_id=session_id,
                agent_id=agent_id,
                statuses=normalized_statuses,
                cursor=cursor,
                limit=bounded_batch,
            )
            if not batch:
                break
            records.extend(batch)
            cursor_token = next_steer_run_cursor(batch, limit=bounded_batch)
            if not cursor_token:
                break
            cursor = decode_steer_run_cursor(cursor_token)
            if cursor is None:
                break
        return records

    def _load_session_record(self, session_id: str) -> RunSession | None:
        normalized_session_id = self._normalize_session_id(session_id)
        checkpoint = self.storage.latest_checkpoint(normalized_session_id)
        fallback: RunSession | None = None
        if checkpoint:
            state = checkpoint.get("state")
            if isinstance(state, dict):
                session_payload = state.get("session")
                if isinstance(session_payload, dict):
                    fallback = self._session_from_state(session_payload)
        row = self.storage.load_session(normalized_session_id)
        if row is not None:
            if fallback is None:
                fallback = RunSession(
                    id=normalized_session_id,
                    project_dir=Path(str(row.get("project_dir", self.project_dir))).resolve(),
                    task=str(row.get("task", "")),
                    locale=str(row.get("locale", self.locale) or self.locale),
                    root_agent_id=str(row.get("root_agent_id", "")),
                    workspace_mode=normalize_workspace_mode(row.get("workspace_mode")),
                )
            return self._session_from_storage_row(row, fallback=fallback)
        return fallback

    def _disabled_project_sync_state(self, session: RunSession) -> dict[str, Any]:
        return {
            "status": "disabled",
            "session_id": session.id,
            "project_dir": str(session.project_dir),
            "workspace_mode": session.workspace_mode.value,
            "added": [],
            "modified": [],
            "deleted": [],
            "last_error": None,
        }

    def project_sync_status(self, session_id: str) -> dict[str, Any] | None:
        normalized_session_id = self._normalize_session_id(session_id)
        session = self._load_session_record(normalized_session_id)
        if session is not None and self._is_direct_workspace_mode(session):
            return self._disabled_project_sync_state(session)
        return self._load_project_sync_state(normalized_session_id)

    def project_sync_preview(
        self,
        session_id: str,
        *,
        max_files: int = 80,
        max_chars: int = 200_000,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        session_record = self._load_session_record(normalized_session_id)
        if session_record is not None and self._is_direct_workspace_mode(session_record):
            raise ValueError(
                f"Session {normalized_session_id} uses direct workspace mode; diff preview is unavailable."
            )
        state = self._load_project_sync_state(normalized_session_id)
        if state is None:
            return {
                "status": "none",
                "session_id": normalized_session_id,
                "added_count": 0,
                "modified_count": 0,
                "deleted_count": 0,
                "files": [],
                "truncated": False,
            }

        session, root_agent, workspace_manager = self._project_sync_context(normalized_session_id)
        workspace = workspace_manager.workspace(root_agent.workspace_id)
        changes = workspace_manager.compute_workspace_changes(root_agent.workspace_id)
        staged_changes = WorkspaceChangeSet(
            added=sorted({str(path) for path in state.get("added", [])}),
            modified=sorted({str(path) for path in state.get("modified", [])}),
            deleted=sorted({str(path) for path in state.get("deleted", [])}),
        )
        if (
            not (changes.added or changes.modified or changes.deleted)
            and (staged_changes.added or staged_changes.modified or staged_changes.deleted)
        ):
            changes = staged_changes

        change_type_by_path: dict[str, str] = {}
        for path in changes.added:
            change_type_by_path[path] = "added"
        for path in changes.modified:
            change_type_by_path[path] = "modified"
        for path in changes.deleted:
            change_type_by_path[path] = "deleted"

        ordered_paths = [*changes.modified, *changes.added, *changes.deleted]
        files: list[dict[str, Any]] = []
        total_chars = 0
        truncated = False
        for path in ordered_paths:
            preview = workspace_manager.build_file_diff_preview(
                workspace.base_snapshot_path / path,
                workspace.path / path,
                path,
            )
            projected = total_chars + len(preview.patch)
            if len(files) >= max_files or projected > max_chars:
                truncated = True
                break
            files.append(
                {
                    "path": path,
                    "change_type": change_type_by_path.get(path, "modified"),
                    "patch": preview.patch,
                    "is_binary": preview.is_binary,
                    "before_size": preview.before_size,
                    "after_size": preview.after_size,
                }
            )
            total_chars = projected

        return {
            "status": str(state.get("status", "none")),
            "session_id": normalized_session_id,
            "project_dir": str(session.project_dir),
            "added_count": len(changes.added),
            "modified_count": len(changes.modified),
            "deleted_count": len(changes.deleted),
            "files": files,
            "truncated": truncated,
        }

    def apply_project_sync(self, session_id: str) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        session_record = self._load_session_record(normalized_session_id)
        if session_record is not None and self._is_direct_workspace_mode(session_record):
            raise ValueError(
                f"Session {normalized_session_id} uses direct workspace mode; apply is unavailable."
            )
        state = self._load_project_sync_state(normalized_session_id)
        if state is None:
            raise ValueError(
                "No staged project sync found for session "
                f"{normalized_session_id}. Run a task and let root finish first."
            )
        status = str(state.get("status", "unknown"))
        if status not in {"pending", "reverted"}:
            raise ValueError(
                f"Session {normalized_session_id} is not waiting for apply (current status: {status})."
            )

        session, root_agent, workspace_manager = self._project_sync_context(normalized_session_id)
        changes = workspace_manager.compute_workspace_changes(root_agent.workspace_id)
        staged_changes = WorkspaceChangeSet(
            added=sorted({str(path) for path in state.get("added", [])}),
            modified=sorted({str(path) for path in state.get("modified", [])}),
            deleted=sorted({str(path) for path in state.get("deleted", [])}),
        )
        use_staged_fallback = False
        if (
            not (changes.added or changes.modified or changes.deleted)
            and (staged_changes.added or staged_changes.modified or staged_changes.deleted)
        ):
            changes = staged_changes
            use_staged_fallback = True
        if not (changes.added or changes.modified or changes.deleted):
            state.update(
                {
                    "status": "none",
                    "added": [],
                    "modified": [],
                    "deleted": [],
                    "applied_at": None,
                    "reverted_at": None,
                    "backup_dir": None,
                    "last_error": None,
                }
            )
            self._save_project_sync_state(normalized_session_id, state)
            result = {
                "status": "none",
                "session_id": normalized_session_id,
                "project_dir": str(session.project_dir),
                "added": 0,
                "modified": 0,
                "deleted": 0,
            }
            self._log_diagnostic(
                "project_sync_apply_skipped",
                session_id=normalized_session_id,
                agent_id=root_agent.id,
                payload=result,
            )
            return result

        backup_dir = self._create_project_sync_backup(
            session_id=normalized_session_id,
            project_dir=session.project_dir,
            modified_paths=changes.modified,
            deleted_paths=changes.deleted,
        )
        try:
            if use_staged_fallback:
                workspace = workspace_manager.workspace(root_agent.workspace_id)
                applied = workspace_manager._apply_changes(
                    source_root=workspace.path,
                    destination_root=session.project_dir,
                    changes=changes,
                )
                if (
                    not (applied.added or applied.modified or applied.deleted)
                    and (changes.added or changes.modified)
                ):
                    missing_paths = [
                        relative_path
                        for relative_path in changes.added + changes.modified
                        if not resolve_in_workspace(workspace.path, relative_path).is_file()
                    ]
                    if missing_paths:
                        raise ValueError(
                            "Staged files are missing in root workspace snapshot: "
                            + ", ".join(missing_paths[:20])
                        )
            else:
                applied = workspace_manager.apply_workspace_changes(
                    root_agent.workspace_id,
                    session.project_dir,
                )
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save_project_sync_state(normalized_session_id, state)
            self._log_diagnostic(
                "project_sync_apply_failed",
                level="error",
                session_id=normalized_session_id,
                agent_id=root_agent.id,
                payload={
                    "project_dir": str(session.project_dir),
                    "backup_dir": str(backup_dir),
                },
                error=exc,
            )
            raise

        state.update(
            {
                "status": "applied",
                "project_dir": str(session.project_dir),
                "added": list(applied.added),
                "modified": list(applied.modified),
                "deleted": list(applied.deleted),
                "applied_at": utc_now(),
                "reverted_at": None,
                "backup_dir": str(backup_dir),
                "last_error": None,
            }
        )
        self._save_project_sync_state(normalized_session_id, state)
        result = {
            "status": "applied",
            "session_id": normalized_session_id,
            "project_dir": str(session.project_dir),
            "added": len(applied.added),
            "modified": len(applied.modified),
            "deleted": len(applied.deleted),
            "backup_dir": str(backup_dir),
        }
        self._log_diagnostic(
            "project_sync_applied",
            session_id=normalized_session_id,
            agent_id=root_agent.id,
            payload=result,
        )
        self._get_logger(normalized_session_id).log(
            session_id=normalized_session_id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="project_sync_applied",
            phase="runtime",
            payload=result,
            workspace_id=root_agent.workspace_id,
        )
        return result

    def undo_project_sync(self, session_id: str) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_id(session_id)
        session_record = self._load_session_record(normalized_session_id)
        if session_record is not None and self._is_direct_workspace_mode(session_record):
            raise ValueError(
                f"Session {normalized_session_id} uses direct workspace mode; undo is unavailable."
            )
        state = self._load_project_sync_state(normalized_session_id)
        if state is None:
            raise ValueError(f"No project sync state found for session {normalized_session_id}.")
        if str(state.get("status", "unknown")) != "applied":
            raise ValueError(
                "Session "
                f"{normalized_session_id} has no applied changes to undo (current status: {state.get('status')})."
            )

        session, root_agent, _ = self._project_sync_context(normalized_session_id)
        backup_dir_text = str(state.get("backup_dir", "")).strip()
        if not backup_dir_text:
            raise ValueError(
                f"Session {normalized_session_id} has no backup directory recorded for undo."
            )
        backup_dir = Path(backup_dir_text).resolve()
        if not backup_dir.exists() or not backup_dir.is_dir():
            raise ValueError(
                f"Backup directory for session {normalized_session_id} does not exist: {backup_dir}"
            )

        added = [str(path) for path in state.get("added", [])]
        modified = [str(path) for path in state.get("modified", [])]
        deleted = [str(path) for path in state.get("deleted", [])]

        removed: list[str] = []
        restored: list[str] = []
        missing_backups: list[str] = []

        for relative_path in added:
            target = resolve_in_workspace(session.project_dir, relative_path)
            if target.is_file() or target.is_symlink():
                target.unlink()
                removed.append(relative_path)
                self._prune_empty_parents(target.parent, session.project_dir)

        for relative_path in modified + deleted:
            backup_path = resolve_in_workspace(backup_dir, relative_path)
            target = resolve_in_workspace(session.project_dir, relative_path)
            if not backup_path.exists() or not backup_path.is_file():
                missing_backups.append(relative_path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, target)
            restored.append(relative_path)

        state.update(
            {
                "status": "reverted",
                "reverted_at": utc_now(),
                "last_error": (
                    ""
                    if not missing_backups
                    else f"Missing backup files for: {', '.join(missing_backups)}"
                ),
            }
        )
        self._save_project_sync_state(normalized_session_id, state)
        result = {
            "status": "reverted",
            "session_id": normalized_session_id,
            "project_dir": str(session.project_dir),
            "removed": len(removed),
            "restored": len(restored),
            "missing_backups": missing_backups,
        }
        self._log_diagnostic(
            "project_sync_reverted",
            session_id=normalized_session_id,
            agent_id=root_agent.id,
            payload=result,
        )
        self._get_logger(normalized_session_id).log(
            session_id=normalized_session_id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="project_sync_reverted",
            phase="runtime",
            payload=result,
            workspace_id=root_agent.workspace_id,
        )
        return result

    def _project_sync_state_path(self, session_id: str) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        return (
            self.paths.existing_session_dir(normalized_session_id) / PROJECT_SYNC_STATE_FILENAME
        )

    def _configure_llm_debug_log(self, session_id: str) -> None:
        if not self.debug or self.llm_client is None:
            return
        normalized_session_id = self._normalize_session_id(session_id)
        debug_dir = ensure_directory(self.paths.session_dir(normalized_session_id, create=True) / "debug")
        log_path = debug_dir / "requests_responses.jsonl"
        if hasattr(self.llm_client, "request_response_log_dir"):
            setattr(self.llm_client, "request_response_log_dir", debug_dir)
        if hasattr(self.llm_client, "request_response_log_path"):
            # Backward-compatible fallback for clients that only support a single file.
            setattr(self.llm_client, "request_response_log_path", log_path)
        self._log_diagnostic(
            "llm_debug_log_configured",
            session_id=normalized_session_id,
            payload={"path": str(log_path), "dir": str(debug_dir)},
        )

    def _load_project_sync_state(self, session_id: str) -> dict[str, Any] | None:
        normalized_session_id = self._normalize_session_id(session_id)
        path = self._project_sync_state_path(normalized_session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse project sync state for session {normalized_session_id}: {exc}"
            ) from exc

    def _save_project_sync_state(self, session_id: str, state: dict[str, Any]) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        path = self._project_sync_state_path(normalized_session_id)
        path.write_text(stable_json_dumps(state), encoding="utf-8")

    def _project_sync_context(
        self, session_id: str
    ) -> tuple[RunSession, AgentNode, WorkspaceManager]:
        normalized_session_id = self._normalize_session_id(session_id)
        checkpoint = self.storage.latest_checkpoint(normalized_session_id)
        if not checkpoint:
            raise ValueError(f"No checkpoint found for session {normalized_session_id}.")
        state = checkpoint["state"]
        session = self._session_from_state(state["session"])
        self.project_dir = session.project_dir.resolve()
        self.tool_executor.set_project_dir(self.project_dir)
        agents_payload = state.get("agents", {})
        root_payload = agents_payload.get(session.root_agent_id)
        if not isinstance(root_payload, dict):
            raise ValueError(
                f"Root agent state is missing in checkpoint for session {normalized_session_id}."
            )
        root_agent = self._agent_from_state(root_payload)
        normalized_workspaces = self._normalize_workspace_state_for_session(
            session_id=normalized_session_id,
            workspace_mode=session.workspace_mode,
            project_dir=session.project_dir,
            workspaces_state=state.get("workspaces"),
        )
        workspace_manager = WorkspaceManager.from_state(
            self.paths.existing_session_dir(normalized_session_id), normalized_workspaces
        )
        return session, root_agent, workspace_manager

    def _stage_project_sync(
        self,
        *,
        session: RunSession,
        root_agent: AgentNode,
        workspace_manager: WorkspaceManager,
    ) -> dict[str, Any]:
        changes = workspace_manager.compute_workspace_changes(root_agent.workspace_id)
        has_changes = bool(changes.added or changes.modified or changes.deleted)
        state = {
            "version": PROJECT_SYNC_STATE_VERSION,
            "session_id": session.id,
            "project_dir": str(self.project_dir),
            "workspace_id": root_agent.workspace_id,
            "status": "pending" if has_changes else "none",
            "added": list(changes.added),
            "modified": list(changes.modified),
            "deleted": list(changes.deleted),
            "staged_at": utc_now(),
            "applied_at": None,
            "reverted_at": None,
            "backup_dir": None,
            "last_error": None,
        }
        self._save_project_sync_state(session.id, state)
        event_payload = {
            "status": state["status"],
            "project_dir": str(self.project_dir),
            "added": len(changes.added),
            "modified": len(changes.modified),
            "deleted": len(changes.deleted),
        }
        self._log_diagnostic(
            "project_sync_staged",
            session_id=session.id,
            agent_id=root_agent.id,
            payload=event_payload,
        )
        self._get_logger(session.id).log(
            session_id=session.id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="project_sync_staged",
            phase="runtime",
            payload=event_payload,
            workspace_id=root_agent.workspace_id,
        )
        return state

    def _create_project_sync_backup(
        self,
        *,
        session_id: str,
        project_dir: Path,
        modified_paths: list[str],
        deleted_paths: list[str],
    ) -> Path:
        normalized_session_id = self._normalize_session_id(session_id)
        backups_root = ensure_directory(
            self.paths.existing_session_dir(normalized_session_id) / PROJECT_SYNC_BACKUPS_DIRNAME
        )
        backup_dir = backups_root / f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=False)

        copied: list[str] = []
        for relative_path in sorted(set([*modified_paths, *deleted_paths])):
            source = resolve_in_workspace(project_dir, relative_path)
            if not source.exists() or not source.is_file():
                continue
            destination = resolve_in_workspace(backup_dir, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative_path)

        manifest = {
            "session_id": normalized_session_id,
            "project_dir": str(project_dir),
            "created_at": utc_now(),
            "copied_paths": copied,
            "modified_paths": list(modified_paths),
            "deleted_paths": list(deleted_paths),
        }
        (backup_dir / "manifest.json").write_text(stable_json_dumps(manifest), encoding="utf-8")
        return backup_dir

    def _prune_empty_parents(self, directory: Path, stop_at: Path) -> None:
        current = directory.resolve()
        stop = stop_at.resolve()
        while current != stop:
            if not current.exists() or not current.is_dir():
                break
            if any(current.iterdir()):
                break
            current.rmdir()
            current = current.parent

    async def _run_session(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
        run_root_agent: bool = True,
        focus_agent_id: str | None = None,
    ) -> None:
        self._set_session_status(
            session,
            SessionStatus.RUNNING,
            reason="session_loop_started",
        )
        session.updated_at = utc_now()
        self.storage.upsert_session(session)
        self._live_session_contexts[session.id] = (
            session,
            agents,
            workspace_manager,
        )
        self._worker_semaphore = asyncio.Semaphore(
            max(1, self.config.runtime.limits.max_active_agents)
        )
        self._runtime_wakeup_event = asyncio.Event()
        self._active_root_tasks = {}
        self._background_root_failures = []
        self._active_worker_tasks = {}
        self._background_worker_failures = []
        self._active_tool_run_tasks = {}
        self._tool_run_shell_streams = {}
        if not self._tool_run_waiters:
            self._tool_run_waiters = {}
        self._signal_runtime_change()
        self._log_diagnostic(
            "session_loop_started",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "root_loop": root_loop,
                "pending_agent_count": len(pending_agent_ids),
                "run_root_agent": bool(run_root_agent),
                "focus_agent_id": str(focus_agent_id or "").strip() or None,
            },
        )
        try:
            await self._rebuild_pending_tool_runs(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=max(root_loop, int(session.loop_index)),
                tracked_pending_ids=None,
            )
            if not run_root_agent:
                normalized_focus_agent_id = str(focus_agent_id or "").strip()
                if not normalized_focus_agent_id:
                    raise ValueError("focus_agent_id is required when run_root_agent=False.")
                self._log_diagnostic(
                    "focus_agent_loop_started",
                    session_id=session.id,
                    agent_id=normalized_focus_agent_id,
                    payload={
                        "root_loop": int(session.loop_index),
                        "focus_pending_count": len(self._runtime_pending_agent_ids(agents)),
                    },
                )
            while session.status == SessionStatus.RUNNING:
                self._raise_background_root_failure()
                self._raise_background_worker_failure()
                if run_root_agent:
                    self._ensure_root_tasks_started(
                        session=session,
                        agents=agents,
                        workspace_manager=workspace_manager,
                    )
                self._ensure_worker_tasks_started(
                    session=session,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=int(session.loop_index),
                )
                if self.interrupt_requested:
                    await self._mark_interrupted(
                        session=session,
                        agents=agents,
                        workspace_manager=workspace_manager,
                        pending_agent_ids=self._runtime_pending_agent_ids(agents),
                        root_loop=int(session.loop_index),
                    )
                    break

                if not self._has_runnable_agents(agents, run_root_agent=run_root_agent):
                    if self._all_agents_terminal(agents):
                        await self._auto_finalize_when_all_agents_done(
                            session=session,
                            agents=agents,
                            workspace_manager=workspace_manager,
                            pending_agent_ids=self._runtime_pending_agent_ids(agents),
                            root_loop=int(session.loop_index),
                        )
                    else:
                        session.updated_at = utc_now()
                        self.storage.upsert_session(session)
                        await self._checkpoint(
                            session=session,
                            agents=agents,
                            workspace_manager=workspace_manager,
                            pending_agent_ids=self._runtime_pending_agent_ids(agents),
                            root_loop=int(session.loop_index),
                        )
                    break
                if session.status != SessionStatus.RUNNING:
                    break
                await self._wait_for_runtime_change()
                if session.status == SessionStatus.RUNNING:
                    session.updated_at = utc_now()
                    self.storage.upsert_session(session)
                    await self._checkpoint(
                        session=session,
                        agents=agents,
                        workspace_manager=workspace_manager,
                        pending_agent_ids=self._runtime_pending_agent_ids(agents),
                        root_loop=int(session.loop_index),
                    )
        except asyncio.CancelledError:
            if self.interrupt_requested and session.status == SessionStatus.RUNNING:
                current_task = asyncio.current_task()
                if current_task is not None and hasattr(current_task, "uncancel"):
                    while current_task.cancelling():
                        current_task.uncancel()
                await self._mark_interrupted(
                    session=session,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    pending_agent_ids=self._runtime_pending_agent_ids(agents),
                    root_loop=int(session.loop_index),
                )
                return
            raise
        except Exception as exc:
            await self._mark_failed(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=self._runtime_pending_agent_ids(agents),
                root_loop=int(session.loop_index),
                error=exc,
            )
            raise
        finally:
            await self._cancel_root_tasks()
            await self._cancel_worker_tasks()
            await self._cancel_tool_run_tasks()
            self._live_session_contexts.pop(session.id, None)
            self._runtime_wakeup_event = None
            self._log_diagnostic(
                "session_loop_finished",
                session_id=session.id,
                agent_id=session.root_agent_id,
                payload={
                    "status": session.status.value,
                    "root_loop": int(session.loop_index),
                    "run_root_agent": bool(run_root_agent),
                    "focus_agent_id": str(focus_agent_id or "").strip() or None,
                },
            )

    def _signal_runtime_change(self) -> None:
        wakeup = self._runtime_wakeup_event
        if wakeup is not None:
            wakeup.set()

    @staticmethod
    def _runtime_pending_agent_ids(agents: dict[str, AgentNode]) -> list[str]:
        return [
            node.id
            for node in sorted(agents.values(), key=lambda item: item.id)
            if node.role == AgentRole.WORKER and Orchestrator._is_active_agent(node)
        ]

    def _has_active_runtime_tasks(self) -> bool:
        return bool(
            self._active_root_tasks
            or self._active_worker_tasks
            or self._active_tool_run_tasks
        )

    def _has_runnable_agents(
        self,
        agents: dict[str, AgentNode],
        *,
        run_root_agent: bool,
    ) -> bool:
        if self._has_active_runtime_tasks():
            return True
        for node in agents.values():
            if not self._is_active_agent(node):
                continue
            if node.role == AgentRole.ROOT and not run_root_agent:
                continue
            return True
        return False

    async def _wait_for_runtime_change(self) -> None:
        wakeup = self._runtime_wakeup_event
        if wakeup is None:
            return
        if wakeup.is_set():
            wakeup.clear()
            return
        await wakeup.wait()
        wakeup.clear()

    def _advance_root_loop(self, session: RunSession, *, root_agent_id: str) -> int:
        session.root_agent_id = root_agent_id
        session.loop_index = max(0, int(session.loop_index)) + 1
        session.updated_at = utc_now()
        self.storage.upsert_session(session)
        return int(session.loop_index)

    def _ensure_root_tasks_started(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        agent_ids: list[str] | None = None,
    ) -> None:
        candidate_ids = agent_ids or self._active_root_ids(agents)
        for agent_id in candidate_ids:
            if agent_id in self._active_root_tasks:
                continue
            root = agents.get(agent_id)
            if root is None or root.role != AgentRole.ROOT:
                continue
            if not self._is_active_agent(root):
                continue
            self._active_root_tasks[agent_id] = asyncio.create_task(
                self._run_root_task(
                    session=session,
                    agent_id=agent_id,
                    agents=agents,
                    workspace_manager=workspace_manager,
                )
            )
            self._log_diagnostic(
                "agent_scheduled",
                session_id=session.id,
                agent_id=agent_id,
                payload={"role": AgentRole.ROOT.value},
            )
            self._signal_runtime_change()

    async def _run_root_task(
        self,
        *,
        session: RunSession,
        agent_id: str,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> None:
        try:
            await self._run_root(
                agents[agent_id],
                agents,
                session,
                workspace_manager,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._background_root_failures.append((agent_id, exc))
        finally:
            self._active_root_tasks.pop(agent_id, None)
            self._log_diagnostic(
                "agent_task_finished",
                session_id=session.id,
                agent_id=agent_id,
                payload={"role": AgentRole.ROOT.value},
            )
            self._signal_runtime_change()

    async def _run_root(
        self,
        agent: AgentNode,
        agents: dict[str, AgentNode],
        session: RunSession,
        workspace_manager: WorkspaceManager,
    ) -> None:
        if not self._is_active_agent(agent):
            return
        tracked_children = [
            child_id
            for child_id in agent.children
            if (child := agents.get(child_id))
            and self._is_active_agent(child)
        ]
        while (
            not self.interrupt_requested
            and session.status == SessionStatus.RUNNING
            and self._is_active_agent(agent)
        ):
            tracked_children = self._consume_finished_child_summaries(
                agent,
                tracked_children,
                agents,
            )
            root_loop = self._advance_root_loop(session, root_agent_id=agent.id)
            root_soft_limit_reached = (
                agent.step_count >= self.config.runtime.limits.max_root_steps
            )
            self._log_diagnostic(
                "root_cycle_started",
                session_id=session.id,
                agent_id=agent.id,
                payload={
                    "root_loop": root_loop,
                    "root_step_count": agent.step_count,
                    "pending_agent_count": len(tracked_children),
                    "root_soft_limit_reached": root_soft_limit_reached,
                    "max_root_steps": self.config.runtime.limits.max_root_steps,
                    "active_root_count": len(self._active_root_ids(agents)),
                },
            )
            tracked_children = await self._run_root_cycle(
                session=session,
                root_agent=agent,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=tracked_children,
                root_loop=root_loop,
            )
            self._signal_runtime_change()
            if (
                self.interrupt_requested
                or session.status != SessionStatus.RUNNING
                or not self._is_active_agent(agent)
            ):
                return
            await asyncio.sleep(0)

    def _raise_background_root_failure(self) -> None:
        if not self._background_root_failures:
            return
        agent_id, exc = self._background_root_failures.pop(0)
        raise RuntimeError(f"Background root {agent_id} failed: {exc}") from exc

    async def _cancel_root_tasks(self) -> None:
        tasks = list(self._active_root_tasks.values())
        if not tasks:
            self._active_root_tasks = {}
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active_root_tasks = {}

    @staticmethod
    def _active_root_ids(agents: dict[str, AgentNode]) -> list[str]:
        return [
            node.id
            for node in sorted(agents.values(), key=lambda item: item.id)
            if node.role == AgentRole.ROOT and Orchestrator._is_active_agent(node)
        ]

    async def _auto_finalize_when_all_agents_done(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
    ) -> None:
        roots = [
            node
            for node in sorted(agents.values(), key=lambda item: item.id)
            if node.role == AgentRole.ROOT
        ]
        summary = ""
        completion_state = "completed"
        if roots:
            last_root = roots[-1]
            summary = str(last_root.summary or "").strip()
            normalized_completion_state = normalize_session_completion_state(
                session_status=SessionStatus.COMPLETED,
                completion_state=str(last_root.completion_status or "completed"),
            )
            completion_state = normalized_completion_state or "completed"
        if not summary:
            summary = "All agents have finished."
        self._set_session_status(
            session,
            SessionStatus.COMPLETED,
            reason="auto_finalized_all_agents_done",
            completion_state=completion_state,
        )
        session.final_summary = summary
        session.follow_up_needed = False
        session.updated_at = utc_now()
        self.storage.upsert_session(session)
        self._log_diagnostic(
            "session_finalized_no_active_agents",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "completion_state": completion_state,
                "root_loop": root_loop,
            },
        )
        self._get_logger(session.id).log(
            session_id=session.id,
            agent_id=session.root_agent_id,
            parent_agent_id=None,
            event_type="session_finalized",
            phase="scheduler",
            payload={
                "user_summary": summary,
                "completion_state": completion_state,
                "follow_up_needed": False,
                "task": session.task,
                "session_status": session.status.value,
            },
            workspace_id=agents[session.root_agent_id].workspace_id
            if session.root_agent_id in agents
            else None,
        )
        await self._checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
        )
        self.tool_executor.cleanup_session_remote_runtime(session.id)

        self._log_diagnostic(
            "focus_agent_loop_finished",
            session_id=session.id,
            agent_id=normalized_focus_agent_id,
            payload={"status": session.status.value, "root_loop": root_loop},
        )

    async def _run_root_cycle(
        self,
        *,
        session: RunSession,
        root_agent: AgentNode,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
    ) -> list[str]:
        pending_agent_ids = self._consume_finished_child_summaries(
            root_agent,
            pending_agent_ids,
            agents,
        )

        async def ask_root_agent(current_agent: AgentNode) -> list[dict[str, Any]]:
            # Refresh pending children right before each LLM request so root can
            # react without waiting for another tool step.
            pending_agent_ids[:] = self._consume_finished_child_summaries(
                current_agent,
                pending_agent_ids,
                agents,
            )
            self._maybe_append_root_soft_limit_reminder(
                session=session,
                root_agent=current_agent,
                root_loop=root_loop,
            )
            return await self._ask_agent(current_agent)

        if self.interrupt_requested:
            return pending_agent_ids
        actions = await ask_root_agent(root_agent)
        if self.interrupt_requested:
            return pending_agent_ids
        action_result = await self._execute_agent_actions(
            session=session,
            agent=root_agent,
            actions=actions,
            agents=agents,
            workspace_manager=workspace_manager,
            root_loop=root_loop,
            tracked_pending_ids=pending_agent_ids,
        )
        if self.interrupt_requested:
            return pending_agent_ids
        if action_result.finish_payload:
            await self._finalize_root(
                session=session,
                root_agent=root_agent,
                payload=action_result.finish_payload,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=pending_agent_ids,
                root_loop=root_loop,
            )
            return pending_agent_ids
        return pending_agent_ids

    async def _run_worker(self, agent: AgentNode, agents: dict[str, AgentNode], session: RunSession, workspace_manager: WorkspaceManager, root_loop: int) -> None:
        if not self._is_active_agent(agent):
            return
        tracked_children = [
            child_id
            for child_id in agent.children
            if (child := agents.get(child_id))
            and self._is_active_agent(child)
        ]

        async def ask_worker_agent(current_agent: AgentNode) -> list[dict[str, Any]]:
            tracked_children[:] = self._consume_finished_child_summaries(
                current_agent,
                tracked_children,
                agents,
            )
            return await self._ask_agent(current_agent)
        while not self.interrupt_requested and self._is_active_agent(agent):
            current_root_loop = max(int(session.loop_index), int(root_loop))
            self._maybe_append_worker_soft_limit_reminder(
                session=session,
                agent=agent,
            )
            actions = await ask_worker_agent(agent)
            if self.interrupt_requested or not self._is_active_agent(agent):
                return
            loop_result = await self._execute_agent_actions(
                session=session,
                agent=agent,
                actions=actions,
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=current_root_loop,
                tracked_pending_ids=tracked_children,
            )
            if self.interrupt_requested or not self._is_active_agent(agent):
                return
            if loop_result.finish_payload:
                await self._complete_worker(
                    session=session,
                    agent=agent,
                    payload=loop_result.finish_payload,
                    workspace_manager=workspace_manager,
                    agents=agents,
                    root_loop=current_root_loop,
                )
                return
            await asyncio.sleep(0)

    def _maybe_append_root_soft_limit_reminder(
        self,
        *,
        session: RunSession,
        root_agent: AgentNode,
        root_loop: int,
    ) -> None:
        max_root_steps = max(1, int(self.config.runtime.limits.max_root_steps))
        if root_agent.step_count < max_root_steps:
            return
        interval = max(
            1,
            int(self.config.runtime.limits.root_soft_limit_reminder_interval),
        )
        raw_last_step = root_agent.metadata.get("root_soft_limit_last_reminder_step")
        try:
            last_step = int(raw_last_step)
        except (TypeError, ValueError):
            last_step = -interval
        if (root_agent.step_count - last_step) < interval:
            return
        reminder_message = {
            "role": "user",
            "content": self._runtime_message("root_loop_force_finalize"),
        }
        root_agent.metadata["root_soft_limit_last_reminder_step"] = root_agent.step_count
        self._append_agent_message(
            root_agent,
            reminder_message,
            None,
            {"exclude_from_context_compression": True},
        )
        self._log_control_message(
            root_agent,
            kind="root_loop_force_finalize",
            content=str(reminder_message.get("content", "")),
        )
        self._log_diagnostic(
            "root_soft_limit_reminder",
            level="warning",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={
                "root_loop": root_loop,
                "root_step_count": root_agent.step_count,
                "max_root_steps": max_root_steps,
                "root_soft_limit_reminder_interval": interval,
            },
        )

    def _maybe_append_worker_soft_limit_reminder(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
    ) -> None:
        max_steps = max(1, int(self.config.runtime.limits.max_agent_steps))
        if agent.step_count < max_steps:
            return
        interval = max(
            1,
            int(self.config.runtime.limits.worker_soft_limit_reminder_interval),
        )
        raw_last_step = agent.metadata.get("worker_soft_limit_last_reminder_step")
        try:
            last_step = int(raw_last_step)
        except (TypeError, ValueError):
            last_step = -interval
        if (agent.step_count - last_step) < interval:
            return
        reminder_message = step_limit_summary_message(
            max_steps=max_steps,
            reason=self._runtime_message("worker_step_limit_reason"),
            prompt_library=self.prompt_library,
            locale=self.locale,
        )
        agent.metadata["worker_soft_limit_last_reminder_step"] = agent.step_count
        self._append_agent_message(
            agent,
            reminder_message,
            None,
            {"exclude_from_context_compression": True},
        )
        self._log_control_message(
            agent,
            kind="worker_soft_limit_reminder",
            content=str(reminder_message.get("content", "")),
        )
        self._log_diagnostic(
            "worker_soft_limit_reminder",
            level="warning",
            session_id=session.id,
            agent_id=agent.id,
            payload={
                "step_count": agent.step_count,
                "max_agent_steps": max_steps,
                "worker_soft_limit_reminder_interval": interval,
            },
        )

    async def _execute_agent_actions(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        actions: list[dict[str, Any]],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None = None,
    ) -> ActionBatchResult:
        finish_payload: dict[str, Any] | None = None
        if tracked_pending_ids is not None:
            remaining = self._consume_finished_child_summaries(agent, tracked_pending_ids, agents)
            tracked_pending_ids[:] = remaining

        ordered_actions = self._order_actions_for_execution(actions)
        has_deferred_compress = any(
            str(action.get("type", "")).strip() == "compress_context"
            for action in ordered_actions
        )

        for action in ordered_actions:
            if self.interrupt_requested:
                break
            if agent.role == AgentRole.WORKER and not self._is_active_agent(agent):
                break
            action_type = str(action.get("type", "")).strip()
            submit_result = await self._submit_tool_run(
                session=session,
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=root_loop,
                tracked_pending_ids=tracked_pending_ids,
                force_wait=has_deferred_compress and action_type not in {"compress_context", "finish"},
            )
            agent_result = submit_result.get("agent_result")
            if not isinstance(agent_result, dict):
                agent_result = {}
            self._append_tool_result(agent, actions, action, agent_result)
            maybe_finish = submit_result.get("finish_payload")
            if isinstance(maybe_finish, dict):
                finish_payload = maybe_finish
                break
        return ActionBatchResult(finish_payload=finish_payload)

    @staticmethod
    def _order_actions_for_execution(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered: list[dict[str, Any]] = []
        deferred_compress: list[dict[str, Any]] = []
        deferred_finish: list[dict[str, Any]] = []
        for action in actions:
            action_type = str(action.get("type", "")).strip()
            if action_type == "compress_context":
                deferred_compress.append(action)
                continue
            if action_type == "finish":
                deferred_finish.append(action)
                continue
            ordered.append(action)
        return [*ordered, *deferred_compress, *deferred_finish]

    async def _ask_agent(self, agent: AgentNode) -> list[dict[str, Any]]:
        self._consume_waiting_steers_for_agent(agent)
        return await self.agent_runtime.ask(
            agent,
            self.llm_client,
            model_override=self._runtime_model_override,
        )

    def _consume_waiting_steers_for_agent(self, agent: AgentNode) -> None:
        waiting_runs = self._list_steer_runs_all(
            session_id=agent.session_id,
            statuses=SteerRunStatus.WAITING.value,
            agent_id=agent.id,
            batch_size=500,
        )
        if not waiting_runs:
            return
        waiting_runs.sort(
            key=lambda item: (
                str(item.get("created_at", "")),
                str(item.get("id", "")),
            )
        )
        delivered_step = max(1, int(agent.step_count) + 1)
        for run in waiting_runs:
            steer_run_id = str(run.get("id", "")).strip()
            if not steer_run_id:
                continue
            content = str(run.get("content", "") or "")
            if not content.strip():
                continue
            transitioned = self.storage.complete_waiting_steer_run(
                session_id=agent.session_id,
                steer_run_id=steer_run_id,
                completed_at=utc_now(),
                delivered_step=delivered_step,
            )
            if transitioned is None:
                continue
            steer_source = str(transitioned.get("source", "")).strip() or "manual"
            steer_source_agent_id = (
                str(transitioned.get("source_agent_id", "")).strip() or USER_STEER_SOURCE_ID
            )
            steer_source_agent_name = (
                str(transitioned.get("source_agent_name", "")).strip()
                or self._steer_user_label()
            )
            steer_message = {"role": "user", "content": content}
            self._append_agent_message(
                agent,
                steer_message,
                steer_message,
                {
                    "source": "steer",
                    "steer_run_id": steer_run_id,
                    "steer_source": steer_source,
                    "steer_source_agent_id": steer_source_agent_id,
                    "steer_source_agent_name": steer_source_agent_name,
                    "delivered_step": delivered_step,
                },
            )
            self._log_agent_event(
                agent,
                event_type="steer_run_updated",
                phase="steer",
                payload={
                    "steer_run_id": steer_run_id,
                    "status": SteerRunStatus.COMPLETED.value,
                    "source": steer_source,
                    "source_agent_id": steer_source_agent_id,
                    "source_agent_name": steer_source_agent_name,
                    "completed_at": transitioned.get("completed_at"),
                    "delivered_step": delivered_step,
                },
            )

    def _set_runtime_model_override(self, model: str | None) -> str:
        normalized = str(model or "").strip()
        config = self.config.llm.openrouter
        config.model = self._base_openrouter_model
        config.coordinator_model = self._base_openrouter_coordinator_model
        config.worker_model = self._base_openrouter_worker_model
        if normalized:
            config.model = normalized
            config.coordinator_model = normalized
            config.worker_model = normalized
            self._runtime_model_override = normalized
            return normalized
        self._runtime_model_override = None
        return config.model_for_role("root")

    async def _submit_tool_run(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None = None,
        force_wait: bool = False,
    ) -> dict[str, Any]:
        action_type = str(action.get("type", "")).strip()
        if not action_type:
            return {
                "agent_result": {
                    "error": "Action is missing a non-empty 'type'.",
                }
            }
        validation_error = self._validate_action_before_submit(agent=agent, action=action)
        if validation_error:
            return {
                "agent_result": {
                    "error": validation_error,
                }
            }
        run = ToolRun(
            id=self._new_tool_run_id(),
            session_id=session.id,
            agent_id=agent.id,
            tool_name=action_type,
            arguments={
                key: value
                for key, value in action.items()
                if key not in {"_tool_call_id", "blocking"}
            },
            status=ToolRunStatus.QUEUED,
            blocking=True,
            created_at=utc_now(),
        )
        with self.storage.batched_writes():
            self.storage.upsert_tool_run(run)
            run.status = ToolRunStatus.RUNNING
            run.status_reason = "started"
            run.started_at = utc_now()
            self.storage.upsert_tool_run(run)
        self._log_agent_event(
            agent,
            event_type="tool_run_submitted",
            phase="tool",
            payload={
                "tool_run_id": run.id,
                "tool_name": run.tool_name,
                "action": _public_action(action),
            },
        )
        self._notify_tool_run_waiters(run.id)
        self._log_agent_event(
            agent,
            event_type="tool_run_updated",
            phase="tool",
            payload={
                "tool_run_id": run.id,
                "tool_name": run.tool_name,
                "status": run.status.value,
            },
        )
        task = asyncio.create_task(
            self._execute_tool_run_background(
                run=run,
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
                session=session,
                root_loop=root_loop,
                tracked_pending_ids=tracked_pending_ids,
            )
        )
        self._active_tool_run_tasks[run.id] = task
        try:
            if action_type == "shell":
                if force_wait:
                    raw_result = await task
                else:
                    inline_wait_seconds = max(
                        0.0,
                        float(self.config.runtime.tools.shell_inline_wait_seconds),
                    )
                    try:
                        raw_result = await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=inline_wait_seconds,
                        )
                    except asyncio.TimeoutError:
                        self._log_diagnostic(
                            "shell_inline_wait_exceeded",
                            level="warning",
                            session_id=session.id,
                            agent_id=agent.id,
                            payload={
                                "tool_run_id": run.id,
                                "inline_wait_seconds": inline_wait_seconds,
                            },
                        )
                        running_result = self._shell_background_running_result(
                            run=run,
                            inline_wait_seconds=inline_wait_seconds,
                        )
                        return {
                            "agent_result": self._project_tool_result(
                                action=action,
                                raw_result=running_result,
                                run_id=run.id,
                            ),
                            "finish_payload": None,
                        }
            else:
                raw_result = await task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            refreshed = self.storage.load_tool_run(run.id)
            if (
                refreshed is not None
                and str(refreshed.get("status", "")) in TERMINAL_TOOL_RUN_STATUSES
            ):
                raw_result = {
                    "error": f"Tool run {run.id} was cancelled.",
                    "tool_run": refreshed,
                }
            else:
                raise
        if not isinstance(raw_result, dict):
            raw_result = {}
        maybe_finish = raw_result.get("finish_payload")
        finish_payload = maybe_finish if isinstance(maybe_finish, dict) else None
        return {
            "agent_result": self._project_tool_result(
                action=action,
                raw_result=raw_result,
                run_id=run.id,
            ),
            "finish_payload": finish_payload,
        }

    async def _execute_tool_run_background(
        self,
        *,
        run: ToolRun,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        session: RunSession,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> dict[str, Any]:
        try:
            return await self._execute_tool_run(
                run=run,
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
                session=session,
                root_loop=root_loop,
                tracked_pending_ids=tracked_pending_ids,
            )
        except asyncio.CancelledError:
            refreshed = self.storage.load_tool_run(run.id)
            current_status = str(refreshed.get("status", "")) if refreshed is not None else ""
            if current_status not in TERMINAL_TOOL_RUN_STATUSES:
                run.status = ToolRunStatus.CANCELLED
                run.status_reason = "cancelled_by_runtime_task"
                run.result = None
                run.error = "Tool run cancelled."
                run.completed_at = utc_now()
                self.storage.upsert_tool_run(run)
                self._clear_shell_stream_output(run.id)
                self._notify_tool_run_waiters(run.id)
                self._log_agent_event(
                    agent,
                    event_type="tool_run_updated",
                    phase="tool",
                    payload={
                        "tool_run_id": run.id,
                        "tool_name": run.tool_name,
                        "status": run.status.value,
                        "error": run.error,
                    },
                )
            raise
        except Exception as exc:
            refreshed = self.storage.load_tool_run(run.id)
            current_status = str(refreshed.get("status", "")) if refreshed is not None else ""
            if current_status not in TERMINAL_TOOL_RUN_STATUSES:
                run.status = ToolRunStatus.FAILED
                run.status_reason = "failed_by_runtime_exception"
                run.result = None
                run.error = str(exc)
                run.completed_at = utc_now()
                self.storage.upsert_tool_run(run)
                self._clear_shell_stream_output(run.id)
                self._notify_tool_run_waiters(run.id)
                self._log_agent_event(
                    agent,
                    event_type="tool_run_updated",
                    phase="tool",
                    payload={
                        "tool_run_id": run.id,
                        "tool_name": run.tool_name,
                        "status": run.status.value,
                        "error": str(exc),
                    },
                )
            return {"error": str(exc)}
        finally:
            self._active_tool_run_tasks.pop(run.id, None)
            self._signal_runtime_change()

    async def _execute_tool_run(
        self,
        *,
        run: ToolRun,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        session: RunSession,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> dict[str, Any]:
        raw_result: dict[str, Any]
        action_type = str(action.get("type", "")).strip()
        if action_type == "shell":
            raw_result = await self._execute_shell_action(
                agent,
                action,
                workspace_manager,
                tool_run_id=run.id,
            )
        elif action_type == "spawn_agent":
            raw_result = await self._execute_spawn_tool_run(
                run=run,
                agent=agent,
                action=action,
                agents=agents,
                session=session,
                workspace_manager=workspace_manager,
                root_loop=root_loop,
                tracked_pending_ids=tracked_pending_ids,
            )
        elif action_type == "steer_agent":
            raw_result = self._steer_agent_tool_result(
                agent=agent,
                action=action,
                agents=agents,
            )
        elif action_type == "list_tool_runs":
            raw_result = self._tool_run_list_result(agent=agent, action=action, agents=agents)
        elif action_type == "get_tool_run":
            raw_result = self._tool_run_get_result(agent=agent, action=action, agents=agents)
        elif action_type == "wait_run":
            raw_result = await self._run_wait_result(
                session=session,
                agent=agent,
                action=action,
                agents=agents,
            )
        elif action_type == "cancel_tool_run":
            raw_result = await self._tool_run_cancel_result(
                session=session,
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
                root_loop=root_loop,
                tracked_pending_ids=tracked_pending_ids,
            )
        elif action_type == "cancel_agent":
            raw_result = await self._execute_read_only_action_with_timeout(
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
            )
            await self._apply_cancel_agent_side_effects(
                session=session,
                requesting_agent=agent,
                action=action,
                raw_result=raw_result,
                agents=agents,
                tracked_pending_ids=tracked_pending_ids,
            )
        elif action_type == "wait_time":
            raw_result = await self._wait_time_result(action=action)
        elif action_type == "compress_context":
            raw_result = await self.agent_runtime.compress_context(
                agent,
                llm_client=self.llm_client,
                reason="manual",
            )
        elif action_type == "finish":
            raw_result = self._finish_tool_result(agent=agent, action=action, agents=agents)
        else:
            raw_result = await self._execute_read_only_action_with_timeout(
                agent=agent,
                action=action,
                agents=agents,
                workspace_manager=workspace_manager,
            )

        refreshed = self.storage.load_tool_run(run.id)
        existing_status = str(refreshed.get("status", "")) if refreshed is not None else ""
        if existing_status in TERMINAL_TOOL_RUN_STATUSES:
            self._clear_shell_stream_output(run.id)
            self._log_diagnostic(
                "tool_run_terminal_write_skipped",
                level="warning",
                session_id=agent.session_id,
                agent_id=agent.id,
                payload={
                    "tool_run_id": run.id,
                    "existing_status": existing_status,
                    "action_type": action_type,
                },
            )
            return raw_result

        error_text = str(raw_result.get("error", "")).strip()
        run.completed_at = utc_now()
        if error_text:
            run.status = ToolRunStatus.FAILED
            run.status_reason = "failed_by_tool_result"
            run.result = None
            run.error = error_text
        else:
            run.status = ToolRunStatus.COMPLETED
            run.status_reason = "completed"
            run.result = json_ready(raw_result)
            run.error = None
        self.storage.upsert_tool_run(run)
        self._clear_shell_stream_output(run.id)
        self._notify_tool_run_waiters(run.id)
        self._log_agent_event(
            agent,
            event_type="tool_run_updated",
            phase="tool",
            payload={
                "tool_run_id": run.id,
                "tool_name": run.tool_name,
                "status": run.status.value,
                "result_preview": truncate_text(stable_json_dumps(json_ready(raw_result)), 1200),
            },
        )
        return raw_result

    async def _execute_spawn_tool_run(
        self,
        *,
        run: ToolRun,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        session: RunSession,
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> dict[str, Any]:
        child_id, spawn_result = await self._spawn_child_with_timeout(
            parent=agent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
        )
        if not child_id:
            return spawn_result
        run.arguments = {
            **run.arguments,
            "child_agent_id": child_id,
        }
        self.storage.upsert_tool_run(run)
        if tracked_pending_ids is not None and child_id not in tracked_pending_ids:
            tracked_pending_ids.append(child_id)
        self._ensure_worker_tasks_started(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            root_loop=root_loop,
            agent_ids=[child_id],
        )
        return {
            "child_agent_id": child_id,
        }

    def _tool_run_list_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        del agents
        default_limit, max_limit = self.config.runtime.tools.list_limit_bounds()
        limit = normalize_tool_run_limit(
            action.get("limit", default_limit),
            default=default_limit,
            minimum=1,
            maximum=max_limit,
        )
        statuses, invalid_statuses = parse_tool_run_status_filters(action.get("status"))
        if invalid_statuses:
            allowed_statuses = sorted(PENDING_TOOL_RUN_STATUSES | TERMINAL_TOOL_RUN_STATUSES)
            return {
                "error": (
                    "list_tool_runs received invalid status filter(s): "
                    + ", ".join(invalid_statuses)
                    + "."
                ),
                "invalid_statuses": invalid_statuses,
                "allowed_statuses": allowed_statuses,
            }
        cursor_value = action.get("cursor")
        cursor_text = (
            str(cursor_value).strip()
            if cursor_value is not None
            else None
        )
        decoded_cursor = decode_tool_run_cursor(cursor_text)
        if cursor_text is not None and decoded_cursor is None:
            return {"error": "list_tool_runs received an invalid 'cursor'."}
        runs = self.storage.list_tool_runs(
            session_id=agent.session_id,
            limit=limit + 1,
            statuses=statuses,
            cursor=decoded_cursor,
        )
        has_more = len(runs) > limit
        page_runs = runs[:limit]
        next_cursor = next_tool_run_cursor(page_runs, limit=limit) if has_more else None
        return {
            "tool_runs": page_runs,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def _tool_run_get_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        del agents
        tool_run_id = str(action.get("tool_run_id", "")).strip()
        if not tool_run_id:
            return {"error": "get_tool_run requires 'tool_run_id'."}
        if self._id_kind(tool_run_id) == "agent_id":
            return {
                "error": (
                    f"Expected tool_run_id (prefix 'toolrun-'), but received agent_id '{tool_run_id}'."
                )
            }
        run = self.storage.load_tool_run(tool_run_id)
        if not run:
            return {"error": f"Tool run {tool_run_id} was not found."}
        if str(run.get("session_id")) != agent.session_id:
            return {"error": f"Tool run {tool_run_id} is outside the current session."}
        return {"tool_run": run}

    def _steer_agent_tool_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        configured_scope = self.config.runtime.tools.steer_agent_scope
        target_agent_id = str(action.get("agent_id", "")).strip()
        if not target_agent_id:
            return {
                "steer_agent_status": False,
                "error": "steer_agent requires 'agent_id'.",
                "configured_scope": configured_scope,
            }
        content = str(action.get("content", "")).strip()
        if not content:
            return {
                "steer_agent_status": False,
                "error": "steer_agent requires a non-empty 'content'.",
                "configured_scope": configured_scope,
            }
        if target_agent_id == agent.id:
            return {
                "steer_agent_status": False,
                "error": "steer_agent cannot target the current agent itself.",
                "configured_scope": configured_scope,
            }
        target = agents.get(target_agent_id)
        if target is None:
            target_row = self._agent_row_for_session(agent.session_id, target_agent_id)
            if target_row is None:
                return {
                    "steer_agent_status": False,
                    "error": f"Agent {target_agent_id} was not found.",
                    "configured_scope": configured_scope,
                }
        if configured_scope == "descendants" and not is_descendant(agent.id, target_agent_id, agents):
            return {
                "steer_agent_status": False,
                "error": (
                    "steer_agent target is outside the configured scope "
                    f"'{configured_scope}'."
                ),
                "configured_scope": configured_scope,
            }
        try:
            steer_run = self.submit_steer_run(
                session_id=agent.session_id,
                agent_id=target_agent_id,
                content=content,
                source="agent_tool",
                source_agent_id=agent.id,
                source_agent_name=agent.name,
            )
        except ValueError as exc:
            return {
                "steer_agent_status": False,
                "error": str(exc),
                "configured_scope": configured_scope,
            }
        return {
            "steer_agent_status": True,
            "steer_run_id": str(steer_run.get("id", "")).strip(),
            "target_agent_id": target_agent_id,
            "status": str(steer_run.get("status", "")).strip(),
            "steer_run": steer_run,
        }

    async def _run_wait_result(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        target_tool_run_id = str(action.get("tool_run_id", "")).strip()
        target_agent_id = str(action.get("agent_id", "")).strip()
        has_tool_run = bool(target_tool_run_id)
        has_agent = bool(target_agent_id)
        if has_tool_run == has_agent:
            return {
                "wait_run_status": False,
                "error": "wait_run requires exactly one of 'tool_run_id' or 'agent_id'.",
            }
        timeout_seconds = self.tool_executor.timeout_seconds_for_action("wait_run")
        started = time.perf_counter()
        while True:
            if has_tool_run:
                run = self.storage.load_tool_run(target_tool_run_id)
                if not run:
                    return {
                        "wait_run_status": False,
                        "error": f"Tool run {target_tool_run_id} was not found.",
                    }
                if str(run.get("session_id")) != agent.session_id:
                    return {
                        "wait_run_status": False,
                        "error": f"Tool run {target_tool_run_id} is outside the current session.",
                    }
                status = str(run.get("status", "")).strip().lower()
                if status in TERMINAL_TOOL_RUN_STATUSES:
                    return {"wait_run_status": True}
            else:
                target = agents.get(target_agent_id)
                if target is None:
                    return {
                        "wait_run_status": False,
                        "error": f"Agent {target_agent_id} was not found.",
                    }
                if target.session_id != session.id:
                    return {
                        "wait_run_status": False,
                        "error": f"Agent {target_agent_id} is outside the current session.",
                    }
                if self._is_terminal_agent(target):
                    return {"wait_run_status": True}
                if target.status == AgentStatus.PAUSED:
                    return {
                        "wait_run_status": False,
                        "error": f"Agent {target_agent_id} is paused and not in a terminal status.",
                    }
            if timeout_seconds > 0 and (time.perf_counter() - started) >= timeout_seconds:
                return {
                    "wait_run_status": False,
                    "timed_out": True,
                    "timeout_seconds": timeout_seconds,
                }
            if has_tool_run:
                waiter = self._tool_run_waiters.setdefault(target_tool_run_id, asyncio.Event())
                try:
                    wait_timeout = (
                        min(0.5, max(0.1, timeout_seconds))
                        if timeout_seconds > 0
                        else 0.5
                    )
                    await asyncio.wait_for(waiter.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    pass
                finally:
                    waiter.clear()
                continue
            await asyncio.sleep(0.2)

    async def _wait_time_result(
        self,
        *,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        validation_error = validate_wait_time_action(action)
        if validation_error:
            return {"wait_time_status": False, "error": validation_error}
        seconds = float(action["seconds"])
        timeout_seconds = self.tool_executor.timeout_seconds_for_action("wait_time")
        try:
            if timeout_seconds > 0:
                await asyncio.wait_for(asyncio.sleep(seconds), timeout=timeout_seconds)
            else:
                await asyncio.sleep(seconds)
        except asyncio.TimeoutError:
            return {
                "wait_time_status": False,
                "timed_out": True,
                "timeout_seconds": timeout_seconds,
            }
        return {"wait_time_status": True}

    async def _tool_run_cancel_result(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> dict[str, Any]:
        del workspace_manager, root_loop
        tool_run_id = str(action.get("tool_run_id", "")).strip()
        if not tool_run_id:
            return {"error": "cancel_tool_run requires 'tool_run_id'."}
        if self._id_kind(tool_run_id) == "agent_id":
            return {
                "error": (
                    f"Expected tool_run_id (prefix 'toolrun-'), but received agent_id '{tool_run_id}'."
                )
            }
        record = self.storage.load_tool_run(tool_run_id)
        if not record:
            return {"error": f"Tool run {tool_run_id} was not found."}
        if str(record.get("session_id")) != session.id:
            return {"error": f"Tool run {tool_run_id} is outside the current session."}
        run = self._tool_run_from_record(record)
        self._log_diagnostic(
            "tool_run_cancel_requested",
            level="warning",
            session_id=session.id,
            agent_id=agent.id,
            payload={
                "tool_run_id": tool_run_id,
                "tool_name": run.tool_name,
                "current_status": run.status.value,
            },
        )
        if run.status.value in TERMINAL_TOOL_RUN_STATUSES:
            self._clear_shell_stream_output(run.id)
            return {
                "final_status": run.status.value,
                "cancelled_agents_count": 0,
            }

        task = self._active_tool_run_tasks.get(tool_run_id)
        cancelled_task = False
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancelled_task = True
            self._active_tool_run_tasks.pop(tool_run_id, None)

        refreshed = self.storage.load_tool_run(tool_run_id)
        run = self._tool_run_from_record(refreshed) if refreshed else run
        if run.status.value not in TERMINAL_TOOL_RUN_STATUSES:
            run.status = ToolRunStatus.CANCELLED
            run.status_reason = f"cancelled_by_agent:{agent.id}"
            run.result = None
            run.error = f"Tool run cancelled by agent {agent.id}."
            run.completed_at = utc_now()
            self.storage.upsert_tool_run(run)
            self._clear_shell_stream_output(run.id)
            self._notify_tool_run_waiters(run.id)
            self._log_agent_event(
                agent,
                event_type="tool_run_updated",
                phase="tool",
                payload={
                    "tool_run_id": run.id,
                    "tool_name": run.tool_name,
                    "status": run.status.value,
                    "error": run.error,
                },
            )

        cancelled_agents: list[str] = []
        cancelled_related_runs: list[str] = []
        if run.tool_name == "spawn_agent":
            child_id = str(run.arguments.get("child_agent_id", "")).strip()
            child = agents.get(child_id) if child_id else None
            if child is not None and not self._is_terminal_agent(child):
                summary_payload = await self._request_cancelled_worker_summary(
                    session=session,
                    worker=child,
                )
                if summary_payload is not None:
                    child.summary = str(summary_payload.get("summary", "")).strip() or child.summary
                    child.next_recommendation = (
                        str(summary_payload.get("next_recommendation", "")).strip()
                        or child.next_recommendation
                    )
                    self.storage.upsert_agent(child)
            cancelled_agents = self._terminate_agent_tree_for_cancel(
                root_agent_id=child_id,
                agents=agents,
                reason=f"Cancelled because spawn tool run {run.id} was cancelled.",
            )
            if cancelled_agents:
                terminated_id_set = set(cancelled_agents)
                if tracked_pending_ids is not None:
                    tracked_pending_ids[:] = [
                        item
                        for item in tracked_pending_ids
                        if item not in terminated_id_set
                    ]
                cancelled_related_runs = await self._cancel_pending_tool_runs_for_agents(
                    session_id=session.id,
                    agent_ids=set(cancelled_agents),
                    skip_tool_run_id=run.id,
                    reason=(
                        "Cancelled because owner/child agent was cancelled by cancel_tool_run."
                    ),
                )

        final_record = self.storage.load_tool_run(tool_run_id)
        final_status = (
            str(final_record.get("status", ""))
            if isinstance(final_record, dict)
            else run.status.value
        )
        self._log_diagnostic(
            "tool_run_cancel_completed",
            level="warning",
            session_id=session.id,
            agent_id=agent.id,
            payload={
                "tool_run_id": tool_run_id,
                "cancelled_task": cancelled_task,
                "final_status": final_status,
                "cancelled_agent_count": len(cancelled_agents),
                "cancelled_related_tool_run_count": len(cancelled_related_runs),
            },
        )
        projected: dict[str, Any] = {
            "final_status": final_status,
            "cancelled_agents_count": len(cancelled_agents),
        }
        return projected

    async def _apply_cancel_agent_side_effects(
        self,
        *,
        session: RunSession,
        requesting_agent: AgentNode,
        action: dict[str, Any],
        raw_result: dict[str, Any],
        agents: dict[str, AgentNode],
        tracked_pending_ids: list[str] | None,
    ) -> None:
        if not bool(raw_result.get("cancel_agent_status", False)):
            return
        target_agent_id = str(action.get("agent_id", "")).strip()
        if not target_agent_id:
            return
        recursive = self._coerce_bool(action.get("recursive"), default=True)
        target_ids = self._cancel_agent_target_ids(
            target_agent_id=target_agent_id,
            recursive=recursive,
            agents=agents,
        )
        if not target_ids:
            return

        reason = f"Cancelled by agent {requesting_agent.id} via cancel_agent."
        cancelled_agent_ids: set[str] = set()
        for target_id in target_ids:
            node = agents.get(target_id)
            if node is None:
                continue
            if node.status in {
                AgentStatus.COMPLETED,
                AgentStatus.FAILED,
                AgentStatus.TERMINATED,
            }:
                continue
            if node.status != AgentStatus.CANCELLED:
                self._set_agent_status(
                    node,
                    AgentStatus.CANCELLED,
                    reason=f"cancel_source:agent:{requesting_agent.id}",
                )
            node.completion_status = "cancelled"
            if not str(node.summary or "").strip():
                node.summary = reason
            self.storage.upsert_agent(node)
            self._log_agent_event(
                node,
                event_type="agent_cancelled",
                phase="scheduler",
                payload={
                    "reason": reason,
                    "cancelled_by_agent_id": requesting_agent.id,
                    "cancel_source": "agent",
                },
            )
            cancelled_agent_ids.add(node.id)

        if not cancelled_agent_ids:
            return
        if tracked_pending_ids is not None:
            tracked_pending_ids[:] = [
                item for item in tracked_pending_ids if item not in cancelled_agent_ids
            ]

        await self._cancel_worker_tasks_for_agents(cancelled_agent_ids)
        await self._cancel_pending_tool_runs_for_agents(
            session_id=session.id,
            agent_ids=cancelled_agent_ids,
            skip_tool_run_id=None,
            reason="Cancelled because owner/child agent was cancelled by cancel_agent.",
        )

    @staticmethod
    def _cancel_agent_target_ids(
        *,
        target_agent_id: str,
        recursive: bool,
        agents: dict[str, AgentNode],
    ) -> list[str]:
        if not target_agent_id:
            return []
        if not recursive:
            return [target_agent_id]
        target_ids: list[str] = []
        seen: set[str] = set()
        stack = [target_agent_id]
        while stack:
            current_id = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            target_ids.append(current_id)
            node = agents.get(current_id)
            if node is None:
                continue
            stack.extend(node.children)
        return target_ids

    async def _cancel_worker_tasks_for_agents(self, agent_ids: set[str]) -> None:
        if not agent_ids:
            return
        tasks: list[asyncio.Task[None]] = []
        for agent_id in agent_ids:
            root_task = self._active_root_tasks.pop(agent_id, None)
            if root_task is not None and not root_task.done():
                root_task.cancel()
                tasks.append(root_task)
            task = self._active_worker_tasks.pop(agent_id, None)
            if task is None or task.done():
                continue
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._signal_runtime_change()

    async def _request_cancelled_worker_summary(
        self,
        *,
        session: RunSession,
        worker: AgentNode,
    ) -> dict[str, Any] | None:
        task = self._active_worker_tasks.pop(worker.id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        if self._is_terminal_agent(worker):
            return None

        reason = (
            "The parent cancelled this subagent. Provide a concise cancellation summary now."
            if self.locale != "zh"
            else "父级已取消该 subagent。请立刻给出精简的取消总结。"
        )
        summary_message = step_limit_summary_message(
            max_steps=self.config.runtime.limits.max_agent_steps,
            reason=reason,
            prompt_library=self.prompt_library,
            locale=self.locale,
        )
        self._append_agent_message(worker, summary_message)
        self._log_control_message(
            worker,
            kind="cancelled_summary_requested",
            content=str(summary_message.get("content", "")),
        )
        self._log_diagnostic(
            "cancelled_summary_requested",
            level="warning",
            session_id=session.id,
            agent_id=worker.id,
            payload={"reason": reason},
        )
        try:
            actions = await self._ask_agent(worker)
        except Exception as exc:
            self._log_diagnostic(
                "cancelled_summary_failed",
                level="warning",
                session_id=session.id,
                agent_id=worker.id,
                payload={"reason": reason},
                error=exc,
            )
            return None
        for action in actions:
            if str(action.get("type", "")).strip() != "finish":
                continue
            validation_error = self._validate_action_before_submit(
                agent=worker,
                action=action,
            )
            if validation_error:
                self._append_tool_result(worker, actions, action, {"error": validation_error})
                continue
            summary = str(action.get("summary", "")).strip()
            if not summary:
                continue
            recommendation = str(action.get("next_recommendation", "")).strip()
            if not recommendation:
                recommendation = "Review cancellation details and decide whether to respawn this branch."
            return {
                "summary": summary,
                "next_recommendation": recommendation,
            }
        self._log_diagnostic(
            "cancelled_summary_missing",
            level="warning",
            session_id=session.id,
            agent_id=worker.id,
            payload={"action_types": [str(action.get("type", "")) for action in actions]},
        )
        return None

    def _terminate_agent_tree_for_cancel(
        self,
        *,
        root_agent_id: str,
        agents: dict[str, AgentNode],
        reason: str,
    ) -> list[str]:
        if not root_agent_id:
            return []
        terminated: list[str] = []
        stack = [root_agent_id]
        while stack:
            agent_id = stack.pop()
            node = agents.get(agent_id)
            if node is None:
                continue
            stack.extend(node.children)
            if self._is_terminal_agent(node):
                continue
            self._set_agent_status(
                node,
                AgentStatus.CANCELLED,
                reason="cancel_source:cancel_tool_run",
            )
            node.completion_status = "cancelled"
            if not node.summary:
                node.summary = reason
            if not node.next_recommendation:
                node.next_recommendation = "Parent should decide whether to respawn or finish."
            self.storage.upsert_agent(node)
            self._log_agent_event(
                node,
                event_type="agent_cancelled",
                phase="scheduler",
                payload={
                    "reason": reason,
                    "cancel_source": "cancel_tool_run",
                },
            )
            root_task = self._active_root_tasks.pop(agent_id, None)
            if root_task is not None and not root_task.done():
                root_task.cancel()
            task = self._active_worker_tasks.pop(agent_id, None)
            if task is not None and not task.done():
                task.cancel()
            terminated.append(agent_id)
        return terminated

    async def _cancel_pending_tool_runs_for_agents(
        self,
        *,
        session_id: str,
        agent_ids: set[str],
        skip_tool_run_id: str | None,
        reason: str,
    ) -> list[str]:
        if not agent_ids:
            return []
        cancelled_run_ids: list[str] = []
        for row in self._list_pending_tool_run_records(session_id):
            run_id = str(row.get("id", "")).strip()
            if not run_id or (skip_tool_run_id and run_id == skip_tool_run_id):
                continue
            run_agent_id = str(row.get("agent_id", "")).strip()
            arguments = row.get("arguments")
            child_id = ""
            if isinstance(arguments, dict):
                child_id = str(arguments.get("child_agent_id", "")).strip()
            if run_agent_id not in agent_ids and child_id not in agent_ids:
                continue
            task = self._active_tool_run_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._active_tool_run_tasks.pop(run_id, None)
            refreshed = self.storage.load_tool_run(run_id)
            if not refreshed:
                continue
            status = str(refreshed.get("status", ""))
            if status in TERMINAL_TOOL_RUN_STATUSES:
                cancelled_run_ids.append(run_id)
                continue
            pending = self._tool_run_from_record(refreshed)
            pending.status = ToolRunStatus.CANCELLED
            pending.status_reason = reason
            pending.result = None
            pending.error = reason
            pending.completed_at = utc_now()
            self.storage.upsert_tool_run(pending)
            self._clear_shell_stream_output(run_id)
            self._notify_tool_run_waiters(run_id)
            cancelled_run_ids.append(run_id)
        return cancelled_run_ids

    def _finish_tool_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        unfinished_children = [
            child_id
            for child_id in agent.children
            if (child := agents.get(child_id))
            and self._is_active_agent(child)
        ]
        if unfinished_children:
            return {
                "error": "Cannot finish while unfinished child agents still exist.",
                "pending_children": unfinished_children,
            }

        summary = str(action.get("summary", "")).strip()
        if not summary:
            return {"error": "finish requires a non-empty 'summary'."}

        status = str(action.get("status", "partial")).strip().lower()
        if agent.role == AgentRole.ROOT:
            if status not in {"completed", "partial"}:
                status = "partial"
            finish_payload = {
                "completion_state": status,
                "user_summary": summary,
            }
            return {
                "status": "accepted",
                "finish_payload": finish_payload,
            }

        if status not in {"completed", "partial", "failed"}:
            status = "partial"
        next_recommendation = str(action.get("next_recommendation", "")).strip()
        if not next_recommendation:
            next_recommendation = "Review this summary and continue with the highest-impact next step."
        finish_payload = {
            "status": status,
            "summary": summary,
            "next_recommendation": next_recommendation,
        }
        return {
            "status": "accepted",
            "finish_payload": finish_payload,
        }

    def _validate_action_before_submit(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
    ) -> str | None:
        action_type = str(action.get("type", "")).strip()
        if action_type == "finish":
            return validate_finish_action(agent.role, action)
        if action_type == "wait_time":
            return validate_wait_time_action(action)
        if action_type == "wait_run":
            return validate_wait_run_action(action)
        if action_type == "compress_context":
            return validate_compress_context_action(action)
        if action_type == "spawn_agent" and "inject_child_summary" in action:
            return "spawn_agent received unsupported field(s): 'inject_child_summary'."
        return None

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
            return default
        return default

    @staticmethod
    def _id_kind(identifier: str) -> str:
        normalized = identifier.strip().lower()
        if normalized.startswith("toolrun-"):
            return "tool_run_id"
        if normalized.startswith("steerrun-"):
            return "steer_run_id"
        if normalized.startswith("agent-"):
            return "agent_id"
        return "unknown"

    def _append_shell_stream_output(
        self,
        *,
        tool_run_id: str,
        channel: str,
        text: str,
    ) -> None:
        normalized_tool_run_id = str(tool_run_id or "").strip()
        if not normalized_tool_run_id:
            return
        phase = str(channel or "").strip().lower()
        if phase not in {"stdout", "stderr"}:
            return
        snippet = self._sanitize_shell_stream_text(str(text or ""))
        if not snippet:
            return
        current = self._tool_run_shell_streams.setdefault(
            normalized_tool_run_id,
            {
                "stdout": "",
                "stderr": "",
            },
        )
        current_text = str(current.get(phase, ""))
        current[phase] = truncate_text(current_text + snippet)

    @staticmethod
    def _sanitize_shell_stream_text(text: str) -> str:
        # Remote dependency bootstrap logs are infrastructure noise for regular
        # shell tool calls; keep them out of user-facing tool output.
        marker = "[opencompany][remote-setup]"
        lines = str(text or "").splitlines(keepends=True)
        if not lines:
            return ""
        filtered = [line for line in lines if marker not in line]
        return "".join(filtered)

    def _shell_stream_snapshot(self, tool_run_id: str) -> dict[str, str]:
        normalized_tool_run_id = str(tool_run_id or "").strip()
        if not normalized_tool_run_id:
            return {"stdout": "", "stderr": ""}
        current = self._tool_run_shell_streams.get(normalized_tool_run_id)
        if not isinstance(current, dict):
            return {"stdout": "", "stderr": ""}
        return {
            "stdout": str(current.get("stdout", "")),
            "stderr": str(current.get("stderr", "")),
        }

    def _clear_shell_stream_output(self, tool_run_id: str) -> None:
        normalized_tool_run_id = str(tool_run_id or "").strip()
        if not normalized_tool_run_id:
            return
        self._tool_run_shell_streams.pop(normalized_tool_run_id, None)

    def _shell_outputs_for_tool_run(self, run: dict[str, Any]) -> tuple[str, str]:
        tool_name = str(run.get("tool_name", "")).strip()
        if tool_name != "shell":
            return "", ""

        status = str(run.get("status", "")).strip().lower()
        if status in PENDING_TOOL_RUN_STATUSES:
            snapshot = self._shell_stream_snapshot(str(run.get("id", "")))
            return snapshot["stdout"], snapshot["stderr"]

        result = run.get("result")
        if isinstance(result, dict):
            return str(result.get("stdout", "")), str(result.get("stderr", ""))
        return "", ""

    def _shell_background_running_result(
        self,
        *,
        run: ToolRun,
        inline_wait_seconds: float,
    ) -> dict[str, Any]:
        snapshot = self._shell_stream_snapshot(run.id)
        run_record = self.storage.load_tool_run(run.id) or {
            "id": run.id,
            "status": ToolRunStatus.RUNNING.value,
            "started_at": run.started_at,
            "created_at": run.created_at,
        }
        duration_ms = tool_run_duration_ms(run_record, now_timestamp=utc_now())
        warning = (
            "shell 未在 "
            f"{inline_wait_seconds:g}s 内完成，已转入后台继续运行。"
            if self.locale == "zh"
            else (
                "Shell command did not finish within "
                f"{inline_wait_seconds:g}s and continues in the background."
            )
        )
        return {
            "tool_run_id": run.id,
            "status": ToolRunStatus.RUNNING.value,
            "background": True,
            "stdout": snapshot["stdout"],
            "stderr": snapshot["stderr"],
            "duration_ms": max(0, int(duration_ms or 0)),
            "warning": warning,
        }

    def _notify_tool_run_waiters(self, tool_run_id: str) -> None:
        waiter = self._tool_run_waiters.get(tool_run_id)
        if waiter is not None:
            waiter.set()

    def _new_tool_run_id(self) -> str:
        return f"toolrun-{uuid.uuid4().hex[:12]}"

    def _new_steer_run_id(self) -> str:
        return f"steerrun-{uuid.uuid4().hex[:12]}"

    def _agent_row_for_session(self, session_id: str, agent_id: str) -> dict[str, Any] | None:
        normalized_session_id = self._normalize_session_id(session_id)
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return None
        for row in self.storage.load_agents(normalized_session_id):
            if str(row.get("id", "")).strip() == normalized_agent_id:
                return row
        return None

    def _log_steer_run_event(
        self,
        *,
        session_id: str,
        agent_row: dict[str, Any] | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        if agent_row is None:
            self._get_logger(normalized_session_id).log(
                session_id=normalized_session_id,
                agent_id=str(payload.get("agent_id", "")).strip() or None,
                parent_agent_id=None,
                event_type=event_type,
                phase="steer",
                payload=payload,
                workspace_id=None,
            )
            return
        metadata: dict[str, Any] = {}
        raw_metadata = agent_row.get("metadata_json")
        if isinstance(raw_metadata, str) and raw_metadata.strip():
            try:
                parsed = json.loads(raw_metadata)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                metadata = parsed
        model = str(metadata.get("model", "")).strip() or None
        self._get_logger(normalized_session_id).log(
            session_id=normalized_session_id,
            agent_id=str(agent_row.get("id", "")).strip() or None,
            parent_agent_id=str(agent_row.get("parent_agent_id", "")).strip() or None,
            event_type=event_type,
            phase="steer",
            payload={
                "agent_name": str(agent_row.get("name", "")).strip(),
                "agent_role": str(agent_row.get("role", "")).strip() or AgentRole.WORKER.value,
                "agent_model": model,
                "agent_status": str(agent_row.get("status", "")).strip(),
                "step_count": int(agent_row.get("step_count", 0) or 0),
                **payload,
            },
            workspace_id=str(agent_row.get("workspace_id", "")).strip() or None,
        )

    def _tool_run_from_record(self, record: dict[str, Any]) -> ToolRun:
        return ToolRun(
            id=str(record.get("id", "")),
            session_id=str(record.get("session_id", "")),
            agent_id=str(record.get("agent_id", "")),
            tool_name=str(record.get("tool_name", "")),
            arguments=(
                record.get("arguments")
                if isinstance(record.get("arguments"), dict)
                else {}
            ),
            status=ToolRunStatus(str(record.get("status", ToolRunStatus.QUEUED.value))),
            blocking=True,
            status_reason=(
                str(record["status_reason"]) if record.get("status_reason") is not None else None
            ),
            parent_run_id=(
                str(record["parent_run_id"]) if record.get("parent_run_id") else None
            ),
            result=record.get("result") if isinstance(record.get("result"), dict) else None,
            error=str(record["error"]) if record.get("error") else None,
            created_at=str(record.get("created_at", "")),
            started_at=str(record["started_at"]) if record.get("started_at") else None,
            completed_at=str(record["completed_at"]) if record.get("completed_at") else None,
        )

    def _project_tool_result(
        self,
        *,
        action: dict[str, Any],
        raw_result: dict[str, Any],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        action_type = str(action.get("type", "")).strip()
        error_text = str(raw_result.get("error", "")).strip()
        include_result = self._coerce_bool(action.get("include_result"), default=False)

        if action_type == "shell":
            if error_text:
                return self._project_error_result(raw_result, error_text)
            if str(raw_result.get("status", "")).strip().lower() == ToolRunStatus.RUNNING.value:
                tool_run_id = str(
                    raw_result.get("tool_run_id")
                    or run_id
                    or ""
                ).strip()
                projected_running: dict[str, Any] = {
                    "status": ToolRunStatus.RUNNING.value,
                    "background": bool(raw_result.get("background", False)),
                    "stdout": str(raw_result.get("stdout", "")),
                    "stderr": str(raw_result.get("stderr", "")),
                    "duration_ms": int(raw_result.get("duration_ms", 0) or 0),
                }
                if tool_run_id:
                    projected_running["tool_run_id"] = tool_run_id
                warning = str(raw_result.get("warning", "")).strip()
                if warning:
                    projected_running["warning"] = warning
                return projected_running
            raw_exit_code = raw_result.get("exit_code")
            try:
                exit_code = int(raw_exit_code) if raw_exit_code is not None else -1
            except (TypeError, ValueError):
                exit_code = -1
            projected: dict[str, Any] = {
                "exit_code": exit_code,
                "stdout": str(raw_result.get("stdout", "")),
                "stderr": str(raw_result.get("stderr", "")),
                "duration_ms": int(raw_result.get("duration_ms", 0) or 0),
            }
            if bool(raw_result.get("timed_out", False)):
                projected["timed_out"] = True
                projected["timeout_seconds"] = raw_result.get("timeout_seconds")
            return projected

        if action_type == "wait_time":
            projected = {
                "wait_time_status": bool(raw_result.get("wait_time_status", False)),
            }
            if bool(raw_result.get("timed_out", False)):
                projected["timed_out"] = True
                projected["timeout_seconds"] = raw_result.get("timeout_seconds")
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "compress_context":
            projected = {
                "compressed": bool(raw_result.get("compressed", False)),
                "reason": str(raw_result.get("reason", "")).strip() or "manual",
                "summary_version": max(0, int(raw_result.get("summary_version", 0) or 0)),
                "message_range": raw_result.get("message_range"),
                "step_range": raw_result.get("step_range"),
                "context_tokens_before": max(
                    0, int(raw_result.get("context_tokens_before", 0) or 0)
                ),
                "context_tokens_after": max(
                    0, int(raw_result.get("context_tokens_after", 0) or 0)
                ),
                "context_limit_tokens": max(
                    0, int(raw_result.get("context_limit_tokens", 0) or 0)
                ),
            }
            if error_text:
                projected["error"] = error_text
            elif "error" in raw_result:
                projected["error"] = str(raw_result.get("error", ""))
            return projected

        if action_type == "list_agent_runs":
            if error_text:
                return self._project_error_result(raw_result, error_text)
            rows = raw_result.get("agent_runs", [])
            projected_rows: list[dict[str, Any]] = []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    projected_rows.append(
                        {
                            "id": str(row.get("id", "")).strip(),
                            "name": str(row.get("name", "")).strip(),
                            "role": str(row.get("role", "")).strip(),
                            "status": str(row.get("status", "")).strip(),
                            "created_at": row.get("created_at"),
                            "summary_short": str(row.get("summary_short", "")),
                            "messages_count": max(0, int(row.get("messages_count", 0) or 0)),
                        }
                    )
            return {
                "agent_runs_count": len(projected_rows),
                "agent_runs": projected_rows,
                "next_cursor": raw_result.get("next_cursor"),
                "has_more": bool(raw_result.get("has_more", False)),
            }

        if action_type == "get_agent_run":
            if error_text:
                return self._project_error_result(raw_result, error_text)
            agent_run = raw_result.get("agent_run")
            if not isinstance(agent_run, dict):
                return self._project_error_result(raw_result, "Agent run not found.")
            messages = raw_result.get("messages")
            projected_messages: list[dict[str, Any]] = []
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        projected_messages.append(dict(message))
            projected: dict[str, Any] = {
                "agent_run": {
                    "id": str(agent_run.get("id", "")).strip(),
                    "name": str(agent_run.get("name", "")).strip(),
                    "role": str(agent_run.get("role", "")).strip(),
                    "status": str(agent_run.get("status", "")).strip(),
                    "created_at": agent_run.get("created_at"),
                    "parent_agent_id": agent_run.get("parent_agent_id"),
                    "children_count": max(0, int(agent_run.get("children_count", 0) or 0)),
                    "step_count": max(0, int(agent_run.get("step_count", 0) or 0)),
                },
                "messages": projected_messages,
            }
            warning = str(raw_result.get("warning", "")).strip()
            if warning:
                projected["warning"] = warning
            next_messages_start = raw_result.get("next_messages_start")
            if isinstance(next_messages_start, (int, float)):
                projected["next_messages_start"] = max(0, int(next_messages_start))
            return projected

        if action_type == "cancel_agent":
            projected = {
                "cancel_agent_status": bool(raw_result.get("cancel_agent_status", False)),
            }
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "steer_agent":
            projected = {
                "steer_agent_status": bool(raw_result.get("steer_agent_status", False)),
            }
            steer_run_id = str(raw_result.get("steer_run_id", "")).strip()
            if steer_run_id:
                projected["steer_run_id"] = steer_run_id
            target_agent_id = str(raw_result.get("target_agent_id", "")).strip()
            if target_agent_id:
                projected["target_agent_id"] = target_agent_id
            status = str(raw_result.get("status", "")).strip()
            if status:
                projected["status"] = status
            configured_scope = str(raw_result.get("configured_scope", "")).strip()
            if configured_scope and not projected["steer_agent_status"]:
                projected["configured_scope"] = configured_scope
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "wait_run":
            projected = {
                "wait_run_status": bool(raw_result.get("wait_run_status", False)),
            }
            if bool(raw_result.get("timed_out", False)):
                projected["timed_out"] = True
                projected["timeout_seconds"] = raw_result.get("timeout_seconds")
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "list_tool_runs":
            raw_runs = raw_result.get("tool_runs", [])
            runs = [
                self._tool_run_overview(run, include_result=False)
                for run in raw_runs
                if isinstance(run, dict)
            ]
            has_more = bool(raw_result.get("has_more", False))
            next_cursor = raw_result.get("next_cursor")
            return {
                "tool_runs_count": len(runs),
                "tool_runs": runs,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

        if action_type == "get_tool_run":
            run = raw_result.get("tool_run")
            if not isinstance(run, dict):
                return self._project_error_result(raw_result, error_text or "Tool run not found.")
            overview = self._tool_run_overview(
                run,
                include_result=include_result,
                include_shell_output=True,
            )
            return {
                "tool_run": overview,
            }

        if action_type == "cancel_tool_run":
            final_status = str(raw_result.get("final_status", "")).strip() or "unknown"
            projected = {
                "final_status": final_status,
                "cancelled_agents_count": max(
                    0,
                    int(
                        raw_result.get("cancelled_agents_count", 0) or 0
                    ),
                ),
            }
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "spawn_agent" and "child_agent_id" in raw_result:
            child_id = str(raw_result.get("child_agent_id", "")).strip()
            projected = {
                "child_agent_id": child_id,
            }
            if run_id:
                projected["tool_run_id"] = run_id
            warning = str(raw_result.get("warning", "")).strip()
            if warning:
                projected["warning"] = warning
            if error_text:
                projected["error"] = error_text
            return projected

        if action_type == "finish":
            accepted = str(raw_result.get("status", "")).strip().lower() == "accepted"
            projected = {
                "accepted": accepted,
            }
            if error_text:
                projected["error"] = error_text
            return projected

        if error_text:
            return self._project_error_result(raw_result, error_text)

        projected_default = dict(raw_result)
        if run_id and action_type == "spawn_agent":
            projected_default.setdefault("tool_run_id", run_id)
        return projected_default

    def _project_error_result(self, raw_result: dict[str, Any], error_text: str) -> dict[str, Any]:
        projected: dict[str, Any] = {
            "error": error_text,
        }
        passthrough_fields = (
            "error_code",
            "next_step_hint",
            "expected_arguments",
            "provided_arguments",
            "available_tools",
            "suggested_tools",
            "timed_out",
            "timeout_seconds",
            "duration_ms",
        )
        for field in passthrough_fields:
            if field in raw_result:
                projected[field] = raw_result[field]
        warning = str(raw_result.get("warning", "")).strip()
        if warning:
            projected["warning"] = warning
        run = raw_result.get("tool_run")
        if isinstance(run, dict):
            projected["tool_run"] = self._tool_run_overview(run, include_result=False)
        return projected

    def _tool_run_overview(
        self,
        run: dict[str, Any],
        *,
        include_result: bool,
        include_shell_output: bool = False,
    ) -> dict[str, Any]:
        duration_ms = tool_run_duration_ms(run)
        error_summary = truncate_text(str(run.get("error", "")).strip(), 240).strip()
        tool_name = str(run.get("tool_name", "")).strip()
        overview: dict[str, Any] = {
            "id": str(run.get("id", "")).strip(),
            "tool_name": tool_name,
            "status": str(run.get("status", "")).strip(),
            "agent_id": str(run.get("agent_id", "")).strip(),
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "duration_ms": duration_ms,
            "error_summary": error_summary or None,
        }
        if include_shell_output and tool_name == "shell":
            stdout, stderr = self._shell_outputs_for_tool_run(run)
            overview["stdout"] = stdout
            overview["stderr"] = stderr
        if include_result and isinstance(run.get("result"), dict):
            overview["result"] = run.get("result")
        return overview

    def _list_pending_tool_run_records(self, session_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: tuple[str, str] | None = None
        statuses = sorted(PENDING_TOOL_RUN_STATUSES)
        while True:
            batch = self.storage.list_tool_runs(
                session_id=session_id,
                statuses=statuses,
                limit=500,
                cursor=cursor,
            )
            if not batch:
                break
            records.extend(batch)
            if len(batch) < 500:
                break
            tail = batch[-1]
            created_at = str(tail.get("created_at", "")).strip()
            run_id = str(tail.get("id", "")).strip()
            if not created_at or not run_id:
                break
            cursor = (created_at, run_id)
        return records

    async def _rebuild_pending_tool_runs(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> None:
        pending_records = self._list_pending_tool_run_records(session.id)
        if not pending_records:
            return
        resumed_count = 0
        skipped_count = 0
        for record in pending_records:
            run = self._tool_run_from_record(record)
            existing_task = self._active_tool_run_tasks.get(run.id)
            if existing_task and not existing_task.done():
                continue
            if existing_task and existing_task.done():
                self._active_tool_run_tasks.pop(run.id, None)

            action_type = run.tool_name.strip()
            if not action_type:
                self._mark_pending_tool_run_cancelled(
                    run,
                    error="Cannot resume tool run with empty tool name.",
                )
                skipped_count += 1
                continue
            if action_type == "spawn_agent":
                resumed = self._resume_spawn_tool_run(
                    session=session,
                    run=run,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=root_loop,
                    tracked_pending_ids=tracked_pending_ids,
                )
                if resumed:
                    resumed_count += 1
                else:
                    skipped_count += 1
                continue

            owner = agents.get(run.agent_id)
            if owner is None:
                self._mark_pending_tool_run_cancelled(
                    run,
                    error=(
                        f"Cannot resume tool run {run.id}: owner agent {run.agent_id} does not exist."
                    ),
                )
                skipped_count += 1
                continue
            if not self._is_active_agent(owner):
                self._mark_pending_tool_run_cancelled(
                    run,
                    error=(
                        f"Cannot resume tool run {run.id}: owner agent {owner.id} is {owner.status.value}."
                    ),
                )
                skipped_count += 1
                continue

            if run.status == ToolRunStatus.QUEUED:
                run.status = ToolRunStatus.RUNNING
                run.status_reason = "resumed_from_pending_queue"
                run.started_at = run.started_at or utc_now()
                self.storage.upsert_tool_run(run)
                self._notify_tool_run_waiters(run.id)
            elif run.status == ToolRunStatus.RUNNING and run.started_at is None:
                run.status_reason = run.status_reason or "resumed_running_tool_run"
                run.started_at = utc_now()
                self.storage.upsert_tool_run(run)
                self._notify_tool_run_waiters(run.id)

            action = (
                dict(run.arguments)
                if isinstance(run.arguments, dict)
                else {}
            )
            action["type"] = action_type
            task = asyncio.create_task(
                self._execute_tool_run_background(
                    run=run,
                    agent=owner,
                    action=action,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    session=session,
                    root_loop=root_loop,
                    tracked_pending_ids=tracked_pending_ids,
                )
            )
            self._active_tool_run_tasks[run.id] = task
            resumed_count += 1
        self._log_diagnostic(
            "pending_tool_runs_rebuilt",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "pending_count": len(pending_records),
                "resumed_count": resumed_count,
                "skipped_count": skipped_count,
            },
        )

    def _resume_spawn_tool_run(
        self,
        *,
        session: RunSession,
        run: ToolRun,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        tracked_pending_ids: list[str] | None,
    ) -> bool:
        parent = agents.get(run.agent_id)
        if parent is None:
            self._mark_pending_tool_run_cancelled(
                run,
                error=(
                    f"Cannot resume spawn tool run {run.id}: parent agent {run.agent_id} does not exist."
                ),
            )
            return False
        arguments = run.arguments if isinstance(run.arguments, dict) else {}
        child_id = str(arguments.get("child_agent_id", "")).strip()
        if not child_id:
            self._mark_pending_tool_run_cancelled(
                run,
                error=(
                    f"Cannot resume spawn tool run {run.id}: missing child_agent_id in arguments."
                ),
            )
            return False
        child = agents.get(child_id)
        if child is None:
            self._mark_pending_tool_run_cancelled(
                run,
                error=(
                    f"Cannot resume spawn tool run {run.id}: child agent {child_id} does not exist."
                ),
            )
            return False
        if run.status == ToolRunStatus.QUEUED:
            run.started_at = run.started_at or utc_now()
        elif run.status == ToolRunStatus.RUNNING and run.started_at is None:
            run.started_at = utc_now()

        run.status = ToolRunStatus.COMPLETED
        run.status_reason = "completed_by_spawn_resume"
        run.result = {"child_agent_id": child_id}
        run.error = None
        run.completed_at = utc_now()
        self.storage.upsert_tool_run(run)
        self._notify_tool_run_waiters(run.id)
        self._log_agent_event(
            parent,
            event_type="tool_run_updated",
            phase="tool",
            payload={
                "tool_run_id": run.id,
                "tool_name": run.tool_name,
                "status": run.status.value,
                "result_preview": truncate_text(stable_json_dumps(json_ready(run.result)), 1200),
            },
        )

        if not self._is_active_agent(child):
            if tracked_pending_ids is not None:
                tracked_pending_ids[:] = [item for item in tracked_pending_ids if item != child_id]
            return True

        if tracked_pending_ids is not None and child_id not in tracked_pending_ids:
            tracked_pending_ids.append(child_id)
        self._ensure_worker_tasks_started(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            root_loop=root_loop,
            agent_ids=[child_id],
        )
        return True

    def _mark_pending_tool_run_cancelled(self, run: ToolRun, *, error: str) -> None:
        if run.status.value in TERMINAL_TOOL_RUN_STATUSES:
            return
        run.status = ToolRunStatus.CANCELLED
        run.status_reason = error
        run.error = error
        run.result = None
        run.completed_at = utc_now()
        self.storage.upsert_tool_run(run)
        self._clear_shell_stream_output(run.id)
        self._notify_tool_run_waiters(run.id)

    @staticmethod
    def _agent_public_status(agent: AgentNode) -> str:
        return agent.status.value

    @staticmethod
    def _agent_model(agent: AgentNode) -> str | None:
        metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
        model = str(metadata.get("model", "")).strip()
        return model or None

    def _consume_finished_child_summaries(
        self,
        agent: AgentNode,
        child_ids: list[str],
        agents: dict[str, AgentNode],
    ) -> list[str]:
        del agent
        remaining: list[str] = []
        seen: set[str] = set()
        for child_id in child_ids:
            if child_id in seen:
                continue
            seen.add(child_id)
            child = agents.get(child_id)
            if child and not self._is_active_agent(child):
                continue
            else:
                remaining.append(child_id)
        return remaining

    def _ensure_worker_tasks_started(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
        agent_ids: list[str] | None = None,
    ) -> None:
        candidate_ids = agent_ids or [
            agent_id
            for agent_id, other in agents.items()
            if other.role == AgentRole.WORKER
            and self._is_active_agent(other)
        ]
        for agent_id in candidate_ids:
            if agent_id in self._active_worker_tasks:
                continue
            worker = agents.get(agent_id)
            if not worker or worker.role != AgentRole.WORKER:
                continue
            if not self._is_active_agent(worker):
                continue
            self._active_worker_tasks[agent_id] = asyncio.create_task(
                self._run_worker_task(
                    session=session,
                    agent_id=agent_id,
                    agents=agents,
                    workspace_manager=workspace_manager,
                    root_loop=root_loop,
                )
            )
            self._log_diagnostic(
                "agent_scheduled",
                session_id=session.id,
                agent_id=agent_id,
                payload={"role": AgentRole.WORKER.value},
            )
            self._signal_runtime_change()

    async def _run_worker_task(
        self,
        *,
        session: RunSession,
        agent_id: str,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        root_loop: int,
    ) -> None:
        try:
            if self._worker_semaphore is None:
                await self._run_worker(
                    agents[agent_id],
                    agents,
                    session,
                    workspace_manager,
                    root_loop,
                )
            else:
                async with self._worker_semaphore:
                    await self._run_worker(
                        agents[agent_id],
                        agents,
                        session,
                        workspace_manager,
                        root_loop,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._background_worker_failures.append((agent_id, exc))
        finally:
            self._active_worker_tasks.pop(agent_id, None)
            self._log_diagnostic(
                "agent_task_finished",
                session_id=session.id,
                agent_id=agent_id,
                payload={"role": AgentRole.WORKER.value},
            )
            self._signal_runtime_change()

    def _raise_background_worker_failure(self) -> None:
        if not self._background_worker_failures:
            return
        agent_id, exc = self._background_worker_failures.pop(0)
        raise RuntimeError(f"Background worker {agent_id} failed: {exc}") from exc

    async def _cancel_worker_tasks(self) -> None:
        tasks = list(self._active_worker_tasks.values())
        if not tasks:
            self._active_worker_tasks = {}
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active_worker_tasks = {}

    async def _cancel_tool_run_tasks(self) -> None:
        tasks = list(self._active_tool_run_tasks.values())
        if not tasks:
            self._active_tool_run_tasks = {}
            self._tool_run_shell_streams = {}
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tool_run_tasks = {}
        self._tool_run_shell_streams = {}

    def _execute_read_only_action(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> dict[str, Any]:
        self.tool_executor.set_project_dir(self.project_dir)
        return self.tool_executor.execute_read_only(
            agent=agent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
        )

    async def _execute_read_only_action_with_timeout(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> dict[str, Any]:
        action_type = str(action.get("type", "tool"))
        timeout_seconds = self.tool_executor.timeout_seconds_for_action(action_type)
        timeout_budget_ms = int(timeout_seconds * 1000)
        started = time.perf_counter()
        public_action = _public_action(action)
        result = self._execute_read_only_action(
            agent=agent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms > timeout_budget_ms:
            result = dict(result)
            result.setdefault(
                "warning",
                (
                    f"Tool '{action_type}' exceeded timeout budget ({timeout_seconds}s) "
                    f"and finished after {duration_ms}ms."
                ),
            )
            result["timeout_budget_exceeded"] = True
            result["timeout_seconds"] = timeout_seconds
            result["duration_ms"] = duration_ms
            self._log_agent_event(
                agent,
                event_type="tool_timeout",
                phase="tool",
                payload={
                    "action": public_action,
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": duration_ms,
                    "budget_exceeded": True,
                },
            )
            self._log_diagnostic(
                "tool_action_timeout_budget_exceeded",
                level="warning",
                session_id=agent.session_id,
                agent_id=agent.id,
                payload={
                    "action_type": action_type,
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": duration_ms,
                    "action": public_action,
                },
            )
        return result

    async def _execute_shell_action(
        self,
        agent: AgentNode,
        action: dict[str, Any],
        workspace_manager: WorkspaceManager,
        *,
        tool_run_id: str | None = None,
    ) -> dict[str, Any]:
        self.tool_executor.set_project_dir(self.project_dir)
        normalized_tool_run_id = str(tool_run_id or "").strip()

        async def _on_stream(channel: str, text: str) -> None:
            if not normalized_tool_run_id:
                return
            self._append_shell_stream_output(
                tool_run_id=normalized_tool_run_id,
                channel=channel,
                text=text,
            )

        return await self.tool_executor.execute_shell(
            agent,
            action,
            workspace_manager,
            stream_listener=_on_stream if normalized_tool_run_id else None,
        )

    def _append_tool_result(
        self,
        agent: AgentNode,
        actions: list[dict[str, Any]],
        action: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> None:
        del actions
        self.agent_runtime.append_tool_result(agent, action, tool_result)

    def _append_agent_message(
        self,
        agent: AgentNode,
        message: dict[str, Any],
        stored_message: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(agent.metadata, dict):
            agent.metadata = {}
        agent.conversation.append(message)
        message_index = len(agent.conversation) - 1
        raw_step_map = agent.metadata.get("message_index_to_step")
        step_map: list[int] = []
        if isinstance(raw_step_map, list):
            for value in raw_step_map:
                try:
                    normalized = int(value)
                except (TypeError, ValueError):
                    normalized = 0
                step_map.append(max(0, normalized))
        while len(step_map) < message_index:
            step_map.append(0)
        step_map.append(max(0, int(agent.step_count)))
        agent.metadata["message_index_to_step"] = step_map
        if metadata and bool(metadata.get("internal", False)):
            raw_indices = agent.metadata.get("internal_message_indices")
            normalized: set[int] = set()
            if isinstance(raw_indices, list):
                for value in raw_indices:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        normalized.add(index)
            normalized.add(message_index)
            agent.metadata["internal_message_indices"] = sorted(normalized)
        if metadata and bool(metadata.get("exclude_from_context_compression", False)):
            raw_indices = agent.metadata.get("compression_excluded_message_indices")
            normalized: set[int] = set()
            if isinstance(raw_indices, list):
                for value in raw_indices:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        normalized.add(index)
            normalized.add(message_index)
            updated = sorted(normalized)
            if len(updated) > 2000:
                updated = updated[-2000:]
            agent.metadata["compression_excluded_message_indices"] = updated
        self._get_message_logger(agent.session_id).append(
            agent,
            stored_message or message,
            metadata,
        )
        self.storage.upsert_agent(agent)

    def _sync_agent_messages(self, agent: AgentNode) -> None:
        self._get_message_logger(agent.session_id).sync_conversation(agent)

    def _sync_session_messages_from_checkpoint(self, session_id: str) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        checkpoint = self.storage.latest_checkpoint(normalized_session_id)
        if not checkpoint:
            return
        for payload in checkpoint["state"].get("agents", {}).values():
            self._sync_agent_messages(self._agent_from_state(payload))

    def _restore_agent_conversations_from_messages(
        self,
        session_id: str,
        agents: dict[str, AgentNode],
    ) -> None:
        logger = self._get_message_logger(session_id)
        for agent in agents.values():
            records = logger.read(agent.id)
            if not records:
                self._sync_agent_messages(agent)
                self.storage.upsert_agent(agent)
                continue
            if not isinstance(agent.metadata, dict):
                agent.metadata = {}
            restored_messages: list[dict[str, Any]] = []
            max_step_count = int(agent.step_count)
            message_index_to_step: list[int] = []
            raw_compression_excluded = agent.metadata.get("compression_excluded_message_indices")
            compression_excluded_indices: set[int] = set()
            if isinstance(raw_compression_excluded, list):
                for value in raw_compression_excluded:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if index >= 0:
                        compression_excluded_indices.add(index)
            for record in sorted(
                records,
                key=lambda item: int(item.get("message_index", -1) or -1),
            ):
                message = record.get("message")
                if isinstance(message, dict):
                    restored_messages.append(message)
                else:
                    role = str(record.get("role", "")).strip() or "assistant"
                    restored_messages.append(
                        {
                            "role": role,
                            "content": str(message or ""),
                        }
                    )
                try:
                    max_step_count = max(max_step_count, int(record.get("step_count", 0) or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    message_index = int(record.get("message_index", -1) or -1)
                except (TypeError, ValueError):
                    message_index = -1
                try:
                    step_value = max(0, int(record.get("step_count", 0) or 0))
                except (TypeError, ValueError):
                    step_value = 0
                if bool(record.get("exclude_from_context_compression", False)) and message_index >= 0:
                    compression_excluded_indices.add(message_index)
                if message_index >= 0:
                    while len(message_index_to_step) <= message_index:
                        message_index_to_step.append(0)
                    message_index_to_step[message_index] = step_value
            if restored_messages:
                agent.conversation = restored_messages
            if message_index_to_step:
                agent.metadata["message_index_to_step"] = message_index_to_step
            if compression_excluded_indices:
                updated = sorted(compression_excluded_indices)
                if len(updated) > 2000:
                    updated = updated[-2000:]
                agent.metadata["compression_excluded_message_indices"] = updated
            agent.step_count = max(int(agent.step_count), max_step_count)
            self.storage.upsert_agent(agent)

    def _spawn_child(
        self,
        *,
        parent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> str | None:
        self.tool_executor.set_project_dir(self.project_dir)
        child_id = self.tool_executor.spawn_child(
            parent=parent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
            new_agent_id=self._new_agent_id,
            worker_initial_message=self._worker_initial_message,
        )
        if child_id:
            child = agents[child_id]
            if child.conversation and isinstance(child.conversation[0], dict):
                first = dict(child.conversation[0])
                first["content"] = self._with_agent_identity_prompt(
                    agent_name=child.name,
                    agent_id=child.id,
                    parent_agent_name=parent.name,
                    parent_agent_id=parent.id,
                    content=str(first.get("content", "")),
                )
                child.conversation[0] = first
                self.storage.upsert_agent(child)
            self._sync_agent_messages(child)
        return child_id

    def _spawn_child_skip_result(
        self,
        *,
        parent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
    ) -> dict[str, Any]:
        public_action = _public_action(action)
        instruction = str(action.get("instruction", "")).strip()
        if not instruction:
            return {
                "warning": (
                    "spawn_agent request was skipped because the action did not include "
                    "a non-empty 'instruction'."
                ),
                "reason": "invalid_action",
                "action": public_action,
            }

        limit = self.config.runtime.limits.max_children_per_agent
        if len(parent.children) >= limit:
            return {
                "warning": (
                    "spawn_agent request was skipped because this agent already reached "
                    "its total child fan-out limit for the session. Terminal children "
                    "still count toward max_children_per_agent."
                ),
                "reason": "max_children_per_agent",
                "action": public_action,
                **child_limit_details(parent, agents, limit),
            }
        duplicate_child = self._find_active_duplicate_child(
            parent=parent,
            agents=agents,
            instruction=instruction,
        )
        if duplicate_child is not None:
            return {
                "warning": (
                    "spawn_agent request was skipped because an active child agent "
                    "already has the same instruction."
                ),
                "reason": "duplicate_child_instruction",
                "action": public_action,
                "existing_child_id": duplicate_child.id,
                "existing_child_status": duplicate_child.status.value,
            }

        return {"warning": "spawn_agent request was skipped.", "action": public_action}

    @staticmethod
    def _find_active_duplicate_child(
        *,
        parent: AgentNode,
        agents: dict[str, AgentNode],
        instruction: str,
    ) -> AgentNode | None:
        normalized = instruction.strip()
        if not normalized:
            return None
        for child_id in parent.children:
            child = agents.get(child_id)
            if not child:
                continue
            if not self._is_active_agent(child):
                continue
            if child.instruction.strip() == normalized:
                return child
        return None

    async def _spawn_child_with_timeout(
        self,
        *,
        parent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> tuple[str | None, dict[str, Any]]:
        timeout_seconds = self.tool_executor.timeout_seconds_for_action("spawn_agent")
        timeout_budget_ms = int(timeout_seconds * 1000)
        started = time.perf_counter()
        skip_result: dict[str, Any] | None = None
        child_id = self._spawn_child(
            parent=parent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        public_action = _public_action(action)
        if duration_ms > timeout_budget_ms:
            self._log_agent_event(
                parent,
                event_type="tool_timeout",
                phase="tool",
                payload={
                    "action": public_action,
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": duration_ms,
                    "budget_exceeded": True,
                },
            )
            self._log_diagnostic(
                "tool_action_timeout_budget_exceeded",
                level="warning",
                session_id=parent.session_id,
                agent_id=parent.id,
                payload={
                    "action_type": "spawn_agent",
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": duration_ms,
                    "action": public_action,
                },
            )
            if child_id:
                return (
                    child_id,
                    {
                        "child_agent_id": child_id,
                        "warning": (
                            f"Tool 'spawn_agent' exceeded timeout budget ({timeout_seconds}s) "
                            f"and finished after {duration_ms}ms."
                        ),
                        "timeout_budget_exceeded": True,
                        "timeout_seconds": timeout_seconds,
                        "duration_ms": duration_ms,
                    },
                )
            if skip_result is None:
                skip_result = self._spawn_child_skip_result(
                    parent=parent,
                    action=action,
                    agents=agents,
                )
            return (
                None,
                {
                    **skip_result,
                    "warning": (
                        f"{skip_result['warning']} "
                        f"The tool also exceeded timeout budget ({timeout_seconds}s, {duration_ms}ms)."
                    ),
                    "timeout_budget_exceeded": True,
                    "timeout_seconds": timeout_seconds,
                    "duration_ms": duration_ms,
                },
            )

        if child_id:
            return child_id, {"child_agent_id": child_id}
        if skip_result is None:
            skip_result = self._spawn_child_skip_result(
                parent=parent,
                action=action,
                agents=agents,
            )
        return None, skip_result

    async def _complete_worker(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        payload: dict[str, Any],
        workspace_manager: WorkspaceManager,
        agents: dict[str, AgentNode],
        root_loop: int,
    ) -> None:
        status = str(payload.get("status", "completed")).strip().lower()
        if status not in {"completed", "partial", "failed"}:
            status = "partial"
        direct_mode = self._is_direct_workspace_mode(session)
        if direct_mode:
            diff_artifact = ""
        else:
            diff = workspace_manager.create_diff_artifact(agent.id, agent.workspace_id)
            diff_artifact = str(diff.artifact_path)
        completion = WorkerCompletion(
            summary=str(payload.get("summary", "")),
            status=status,
            next_recommendation=str(payload.get("next_recommendation", "")),
            diff_artifact=diff_artifact,
        )
        workspace = workspace_manager.workspace(agent.workspace_id)
        parent_workspace_id = workspace.parent_workspace_id
        if (
            not direct_mode
            and parent_workspace_id
            and completion.status in {"completed", "partial"}
        ):
            parent_workspace = workspace_manager.workspace(parent_workspace_id)
            try:
                async with self._workspace_merge_lock:
                    changes = await asyncio.to_thread(
                        workspace_manager.apply_workspace_changes,
                        agent.workspace_id,
                        parent_workspace.path,
                    )
                self._log_diagnostic(
                    "workspace_changes_promoted",
                    session_id=session.id,
                    agent_id=agent.id,
                    payload={
                        "target_workspace_id": parent_workspace_id,
                        "added": len(changes.added),
                        "modified": len(changes.modified),
                        "deleted": len(changes.deleted),
                    },
                )
            except Exception as exc:
                completion.status = "failed"
                completion.summary = truncate_text(
                    (
                        completion.summary
                        + "\n\nRuntime failed to promote worker changes to parent workspace: "
                        + str(exc)
                    ).strip(),
                    2000,
                )
                completion.next_recommendation = (
                    "Inspect diagnostics, then retry the task or resume from checkpoint."
                )
                self._log_diagnostic(
                    "workspace_promotion_failed",
                    level="error",
                    session_id=session.id,
                    agent_id=agent.id,
                    payload={
                        "workspace_id": agent.workspace_id,
                        "target_workspace_id": parent_workspace_id,
                    },
                    error=exc,
                )
        target_status = (
            AgentStatus.COMPLETED if completion.status != "failed" else AgentStatus.FAILED
        )
        self._set_agent_status(
            agent,
            target_status,
            reason=f"worker_finished:{completion.status}",
        )
        agent.summary = completion.summary
        agent.next_recommendation = completion.next_recommendation
        agent.diff_artifact = completion.diff_artifact
        agent.completion_status = completion.status
        self.storage.upsert_agent(agent)
        self._log_diagnostic(
            "worker_completed",
            session_id=session.id,
            agent_id=agent.id,
            payload={
                "status": completion.status,
                "diff_artifact": completion.diff_artifact,
            },
        )
        self._log_agent_event(
            agent,
            event_type="agent_completed",
            phase="scheduler",
            payload=json_ready(asdict(completion)),
        )
        await self._checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=[],
            root_loop=root_loop,
        )

    async def _finalize_root(
        self,
        *,
        session: RunSession,
        root_agent: AgentNode,
        payload: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
    ) -> None:
        completion_state = str(payload.get("completion_state", "partial")).strip().lower()
        if completion_state not in {"completed", "partial"}:
            completion_state = "partial"
        finalization = RootFinalization(
            user_summary=str(payload.get("user_summary", "")),
            completion_state=completion_state,
            follow_up_needed=bool(payload.get("follow_up_needed", False)),
        )
        self._set_agent_status(
            root_agent,
            AgentStatus.COMPLETED,
            reason="root_finalized",
        )
        root_agent.summary = finalization.user_summary
        root_agent.completion_status = finalization.completion_state
        self.storage.upsert_agent(root_agent)
        self._log_agent_event(
            root_agent,
            event_type="agent_completed",
            phase="scheduler",
            payload={
                "status": finalization.completion_state,
                "summary": finalization.user_summary,
                "next_recommendation": "",
                "diff_artifact": "",
            },
        )
        has_other_active_agents = any(
            self._is_active_agent(node)
            for node in agents.values()
            if node.id != root_agent.id
        )
        if has_other_active_agents:
            session.updated_at = utc_now()
            self.storage.upsert_session(session)
            self._log_diagnostic(
                "root_finalized_session_continues",
                session_id=session.id,
                agent_id=root_agent.id,
                payload={
                    "completion_state": finalization.completion_state,
                    "other_active_agent_count": sum(
                        1
                        for node in agents.values()
                        if node.id != root_agent.id and self._is_active_agent(node)
                    ),
                },
            )
            await self._checkpoint(
                session=session,
                agents=agents,
                workspace_manager=workspace_manager,
                pending_agent_ids=pending_agent_ids,
                root_loop=root_loop,
            )
            return
        if (
            finalization.completion_state in {"completed", "partial"}
            and not self._is_direct_workspace_mode(session)
        ):
            try:
                staged = self._stage_project_sync(
                    session=session,
                    root_agent=root_agent,
                    workspace_manager=workspace_manager,
                )
                if staged["status"] == "pending":
                    finalization.follow_up_needed = True
                    confirmation_note = (
                        f"Project changes are staged but not yet written to {self.project_dir}. "
                        f"Confirm apply with `opencompany apply {session.id}`. "
                        f"After apply, you can roll back with `opencompany undo {session.id}`."
                    )
                    if finalization.user_summary.strip():
                        finalization.user_summary = (
                            finalization.user_summary.rstrip() + "\n\n" + confirmation_note
                        )
                    else:
                        finalization.user_summary = confirmation_note
            except Exception as exc:
                finalization.completion_state = "partial"
                finalization.follow_up_needed = True
                finalization.user_summary = truncate_text(
                    (
                        finalization.user_summary
                        + "\n\nRuntime failed to stage workspace changes for user confirmation: "
                        + str(exc)
                    ).strip(),
                    4000,
                )
                self._log_diagnostic(
                    "project_sync_stage_failed",
                    level="error",
                    session_id=session.id,
                    agent_id=root_agent.id,
                    payload={"project_dir": str(self.project_dir)},
                    error=exc,
                )
        self._set_session_status(
            session,
            SessionStatus.COMPLETED,
            reason="root_finalized",
            completion_state=finalization.completion_state,
        )
        session.final_summary = finalization.user_summary
        session.follow_up_needed = finalization.follow_up_needed
        session.updated_at = utc_now()
        self.storage.upsert_agent(root_agent)
        self.storage.upsert_session(session)
        self._log_diagnostic(
            "session_finalized",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={
                "completion_state": finalization.completion_state,
                "follow_up_needed": finalization.follow_up_needed,
            },
        )
        self._get_logger(session.id).log(
            session_id=session.id,
            agent_id=root_agent.id,
            parent_agent_id=None,
            event_type="session_finalized",
            phase="scheduler",
            payload=json_ready(
                {
                    **asdict(finalization),
                    "task": session.task,
                    "session_status": session.status.value,
                }
            ),
            workspace_id=root_agent.workspace_id,
        )
        await self._checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
        )

    async def _auto_finalize_root(
        self,
        *,
        session: RunSession,
        root_agent: AgentNode,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        reason: str,
        root_loop: int,
    ) -> None:
        child_summaries = self._child_summaries(root_agent, agents)
        summary = (
            reason
            + "\n\nCurrent child progress:\n"
            + stable_json_dumps(child_summaries or [{"summary": "No child summaries recorded."}])
        )
        self._log_diagnostic(
            "root_auto_finalize_triggered",
            level="warning",
            session_id=session.id,
            agent_id=root_agent.id,
            payload={"reason": reason, "root_loop": root_loop},
        )
        await self._finalize_root(
            session=session,
            root_agent=root_agent,
            payload={
                "user_summary": summary,
                "completion_state": "partial",
                "follow_up_needed": True,
            },
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=[],
            root_loop=root_loop,
        )

    async def _mark_interrupted(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
    ) -> None:
        terminated_agent_ids = self._terminate_active_agents(
            session=session,
            agents=agents,
            reason="Session interrupted by user request.",
        )
        cancelled_tool_run_ids = await self._cancel_pending_tool_runs_for_agents(
            session_id=session.id,
            agent_ids=set(terminated_agent_ids),
            skip_tool_run_id=None,
            reason="Cancelled because session was interrupted.",
        )
        pending_agent_ids = [
            agent_id
            for agent_id in pending_agent_ids
            if agent_id in agents and self._is_active_agent(agents[agent_id])
        ]
        self._set_session_status(
            session,
            SessionStatus.INTERRUPTED,
            reason="user_interrupt",
        )
        session.updated_at = utc_now()
        self.storage.upsert_session(session)
        self._log_diagnostic(
            "session_interrupted",
            level="warning",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "pending_agent_count": len(pending_agent_ids),
                "terminated_agent_count": len(terminated_agent_ids),
                "cancelled_tool_run_count": len(cancelled_tool_run_ids),
                "root_loop": root_loop,
            },
        )
        self._get_logger(session.id).log(
            session_id=session.id,
            agent_id=session.root_agent_id,
            parent_agent_id=None,
            event_type="session_interrupted",
            phase="runtime",
            payload={
                "pending_agent_ids": pending_agent_ids,
                "terminated_agent_ids": terminated_agent_ids,
                "cancelled_tool_run_ids": cancelled_tool_run_ids,
                "task": session.task,
                "session_status": session.status.value,
            },
            workspace_id=agents[session.root_agent_id].workspace_id,
        )
        await self._checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
            interrupted=True,
        )
        self.tool_executor.cleanup_session_remote_runtime(session.id)

    def _terminate_active_agents(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        reason: str,
    ) -> list[str]:
        terminated_agent_ids: list[str] = []
        for agent in sorted(agents.values(), key=lambda node: node.id):
            if not self._is_active_agent(agent):
                continue
            previous_status = agent.status
            self._set_agent_status(
                agent,
                AgentStatus.TERMINATED,
                reason="terminated_by_interrupt",
            )
            agent.completion_status = None
            self.storage.upsert_agent(agent)
            terminated_agent_ids.append(agent.id)
            self._log_agent_event(
                agent,
                event_type="agent_terminated",
                phase="runtime",
                payload={
                    "reason": reason,
                    "previous_status": previous_status.value,
                    "cancel_source": "interrupt",
                },
            )
        if terminated_agent_ids:
            self._log_diagnostic(
                "agents_terminated",
                session_id=session.id,
                agent_id=session.root_agent_id,
                payload={
                    "reason": reason,
                    "agent_ids": terminated_agent_ids,
                },
            )
        return terminated_agent_ids

    async def _mark_failed(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
        error: Exception,
    ) -> None:
        self._set_session_status(
            session,
            SessionStatus.FAILED,
            reason="session_failed",
        )
        session.final_summary = f"Session failed: {error}"
        session.follow_up_needed = True
        session.updated_at = utc_now()
        root_agent = agents[session.root_agent_id]
        if not self._is_terminal_agent(root_agent):
            self._set_agent_status(
                root_agent,
                AgentStatus.FAILED,
                reason="session_failed",
            )
            root_agent.summary = truncate_text(str(error), 500)
            root_agent.completion_status = "failed"
            self.storage.upsert_agent(root_agent)
        self.storage.upsert_session(session)
        self._log_diagnostic(
            "session_failed",
            level="error",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={"pending_agent_count": len(pending_agent_ids), "root_loop": root_loop},
            error=error,
        )
        self._get_logger(session.id).log(
            session_id=session.id,
            agent_id=session.root_agent_id,
            parent_agent_id=None,
            event_type="session_failed",
            phase="runtime",
            payload={
                "error": str(error),
                "error_type": error.__class__.__name__,
                "pending_agent_ids": pending_agent_ids,
                "root_loop": root_loop,
                "task": session.task,
                "session_status": session.status.value,
            },
            workspace_id=root_agent.workspace_id,
        )
        await self._checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
        )
        self.tool_executor.cleanup_session_remote_runtime(session.id)

    def _session_from_storage_row(
        self,
        row: dict[str, Any],
        *,
        fallback: RunSession,
    ) -> RunSession:
        config_snapshot = fallback.config_snapshot
        raw_config = row.get("config_snapshot_json")
        if isinstance(raw_config, str) and raw_config.strip():
            try:
                parsed = json.loads(raw_config)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                config_snapshot = parsed
        raw_status = str(row.get("status", fallback.status.value)).strip().lower()
        try:
            status = SessionStatus(raw_status)
        except ValueError:
            status = fallback.status
        return RunSession(
            id=str(row.get("id", fallback.id) or fallback.id),
            project_dir=Path(str(row.get("project_dir", fallback.project_dir))),
            task=str(row.get("task", fallback.task) or fallback.task),
            locale=str(row.get("locale", fallback.locale) or fallback.locale),
            root_agent_id=str(row.get("root_agent_id", fallback.root_agent_id) or fallback.root_agent_id),
            workspace_mode=normalize_workspace_mode(
                row.get("workspace_mode", fallback.workspace_mode.value)
            ),
            status=status,
            status_reason=(
                str(row.get("status_reason"))
                if row.get("status_reason") is not None
                else fallback.status_reason
            ),
            created_at=str(row.get("created_at", fallback.created_at) or fallback.created_at),
            updated_at=str(row.get("updated_at", fallback.updated_at) or fallback.updated_at),
            loop_index=int(row.get("loop_index", fallback.loop_index) or fallback.loop_index),
            final_summary=(
                str(row.get("final_summary"))
                if row.get("final_summary") is not None
                else fallback.final_summary
            ),
            completion_state=normalize_session_completion_state(
                session_status=status,
                completion_state=(
                    str(row.get("completion_state"))
                    if row.get("completion_state") is not None
                    else fallback.completion_state
                ),
            ),
            follow_up_needed=bool(int(row.get("follow_up_needed", int(fallback.follow_up_needed)) or 0)),
            config_snapshot=config_snapshot,
        )

    def _pause_active_agents(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        reason: str,
    ) -> list[str]:
        paused_agent_ids: list[str] = []
        for agent in sorted(agents.values(), key=lambda node: node.id):
            if not self._is_active_agent(agent):
                continue
            previous_status = agent.status
            self._set_agent_status(
                agent,
                AgentStatus.PAUSED,
                reason=reason,
            )
            self.storage.upsert_agent(agent)
            paused_agent_ids.append(agent.id)
            self._log_agent_event(
                agent,
                event_type="agent_paused",
                phase="runtime",
                payload={
                    "reason": reason,
                    "previous_status": previous_status.value,
                },
            )
        if paused_agent_ids:
            self._log_diagnostic(
                "agents_paused",
                session_id=session.id,
                agent_id=session.root_agent_id,
                payload={
                    "reason": reason,
                    "agent_ids": paused_agent_ids,
                },
            )
        return paused_agent_ids

    def _cancel_pending_tool_runs_for_agents_sync(
        self,
        *,
        session_id: str,
        agent_ids: set[str],
        skip_tool_run_id: str | None,
        reason: str,
    ) -> list[str]:
        if not agent_ids:
            return []
        cancelled_run_ids: list[str] = []
        for row in self._list_pending_tool_run_records(session_id):
            run_id = str(row.get("id", "")).strip()
            if not run_id or (skip_tool_run_id and run_id == skip_tool_run_id):
                continue
            run_agent_id = str(row.get("agent_id", "")).strip()
            arguments = row.get("arguments")
            child_id = ""
            if isinstance(arguments, dict):
                child_id = str(arguments.get("child_agent_id", "")).strip()
            if run_agent_id not in agent_ids and child_id not in agent_ids:
                continue
            task = self._active_tool_run_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                self._active_tool_run_tasks.pop(run_id, None)
            refreshed = self.storage.load_tool_run(run_id)
            if not refreshed:
                continue
            status = str(refreshed.get("status", ""))
            if status in TERMINAL_TOOL_RUN_STATUSES:
                cancelled_run_ids.append(run_id)
                continue
            pending = self._tool_run_from_record(refreshed)
            pending.status = ToolRunStatus.CANCELLED
            pending.status_reason = reason
            pending.result = None
            pending.error = reason
            pending.completed_at = utc_now()
            self.storage.upsert_tool_run(pending)
            self._clear_shell_stream_output(run_id)
            self._notify_tool_run_waiters(run_id)
            cancelled_run_ids.append(run_id)
        return cancelled_run_ids

    def _save_checkpoint(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
        interrupted: bool = False,
    ) -> int:
        del pending_agent_ids
        derived_pending_agent_ids = self._runtime_pending_agent_ids(agents)
        state = CheckpointState(
            session=self._session_state(session),
            agents={agent_id: self._agent_state(agent) for agent_id, agent in agents.items()},
            workspaces=workspace_manager.serialize(),
            pending_agent_ids=list(derived_pending_agent_ids),
            pending_tool_run_ids=self.storage.pending_tool_run_ids(session.id),
            root_loop=root_loop,
            interrupted=interrupted,
        )
        seq = self.storage.save_checkpoint(session.id, utc_now(), state)
        self.storage.replace_pending_agents(session.id, list(derived_pending_agent_ids))
        self._log_diagnostic(
            "checkpoint_saved",
            session_id=session.id,
            agent_id=session.root_agent_id,
            payload={
                "checkpoint_seq": seq,
                "pending_agent_count": len(derived_pending_agent_ids),
                "root_loop": root_loop,
                "interrupted": interrupted,
            },
        )
        return seq

    async def _checkpoint(
        self,
        *,
        session: RunSession,
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        pending_agent_ids: list[str],
        root_loop: int,
        interrupted: bool = False,
    ) -> int:
        return self._save_checkpoint(
            session=session,
            agents=agents,
            workspace_manager=workspace_manager,
            pending_agent_ids=pending_agent_ids,
            root_loop=root_loop,
            interrupted=interrupted,
        )

    def _session_state(self, session: RunSession) -> dict[str, Any]:
        return session_state(session)

    def _agent_state(self, agent: AgentNode) -> dict[str, Any]:
        return agent_state(agent)

    def _session_from_state(self, payload: dict[str, Any]) -> RunSession:
        return session_from_state(payload)

    def _agent_from_state(self, payload: dict[str, Any]) -> AgentNode:
        return agent_from_state(payload)

    def _new_agent_id(self) -> str:
        return f"agent-{uuid.uuid4().hex[:10]}"

    def _new_unique_agent_id(self, agents: dict[str, AgentNode]) -> str:
        while True:
            candidate = self._new_agent_id()
            if candidate not in agents:
                return candidate

    def _root_initial_message(self, task: str) -> str:
        return root_initial_message(
            task,
            self.project_dir,
            self.config.runtime.limits,
            prompt_library=self.prompt_library,
            locale=self.locale,
        )

    def _worker_initial_message(self, instruction: str, workspace_path: Path) -> str:
        sanitized_instruction = self._redact_project_dir(instruction)
        return worker_initial_message(
            sanitized_instruction,
            workspace_path,
            prompt_library=self.prompt_library,
            locale=self.locale,
        )

    def _redact_project_dir(self, text: str) -> str:
        replacement = "<目标项目>" if self.locale == "zh" else "<target-project>"
        candidates = {
            str(self.project_dir),
            self.project_dir.as_posix(),
            str(self.project_dir.resolve()),
            self.project_dir.resolve().as_posix(),
        }
        for candidate in tuple(candidates):
            if candidate.startswith("/private/"):
                candidates.add(candidate.removeprefix("/private"))
        sanitized = text
        for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
            sanitized = sanitized.replace(candidate, replacement)
        return sanitized

    def _runtime_message(self, key: str, **values: Any) -> str:
        return self.prompt_library.render_runtime_message(key, self.locale, **values)

    def _child_summaries(self, parent: AgentNode, agents: dict[str, AgentNode]) -> list[dict[str, Any]]:
        return child_summaries(parent, agents)

    def _log_agent_event(
        self,
        agent: AgentNode,
        *,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        self._get_logger(agent.session_id).log(
            session_id=agent.session_id,
            agent_id=agent.id,
            parent_agent_id=agent.parent_agent_id,
            event_type=event_type,
            phase=phase,
            payload={
                "agent_name": agent.name,
                "agent_role": agent.role.value,
                "agent_model": self._agent_model(agent),
                "agent_status": agent.status.value,
                "step_count": agent.step_count,
                **payload,
            },
            workspace_id=agent.workspace_id,
        )

    def _log_control_message(
        self,
        agent: AgentNode,
        *,
        kind: str,
        content: str,
    ) -> None:
        self._log_agent_event(
            agent,
            event_type="control_message",
            phase="scheduler",
            payload={"kind": kind, "content": content},
        )

    def _get_logger(self, session_id: str) -> StructuredLogger:
        session_id = self._normalize_session_id(session_id)
        logger = self._loggers.get(session_id)
        if logger:
            return logger
        logger = StructuredLogger(
            self.storage,
            self.paths.session_logs_path(session_id, self.config.logging.jsonl_filename),
            diagnostic_logger=self.diagnostics,
        )
        if hasattr(self, "_subscriber"):
            logger.subscribe(self._subscriber)
        self._loggers[session_id] = logger
        return logger

    def _get_message_logger(self, session_id: str) -> AgentMessageLogger:
        session_id = self._normalize_session_id(session_id)
        logger = self._message_loggers.get(session_id)
        if logger:
            return logger
        logger = AgentMessageLogger(self.paths.existing_session_dir(session_id))
        self._message_loggers[session_id] = logger
        return logger

    def _append_agent_summary_record(self, agent: AgentNode, record: dict[str, Any]) -> None:
        session_dir = self.paths.existing_session_dir(agent.session_id)
        append_jsonl(session_dir / f"{agent.id}_summaries.jsonl", record)

    def _log_diagnostic(
        self,
        event_type: str,
        *,
        level: str = "info",
        session_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
        message: str = "",
    ) -> None:
        self.diagnostics.log(
            component="orchestrator",
            event_type=event_type,
            level=level,
            session_id=session_id or self.latest_session_id,
            agent_id=agent_id,
            message=message,
            payload=payload,
            error=error,
        )

def _public_action(action: dict[str, Any]) -> dict[str, Any]:
    return dict(action)
