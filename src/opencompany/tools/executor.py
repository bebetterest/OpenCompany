from __future__ import annotations

import asyncio
import posixpath
from dataclasses import asdict
from datetime import UTC, datetime
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from opencompany.config import OpenCompanyConfig
from opencompany.models import (
    AgentNode,
    AgentRole,
    AgentStatus,
    RemoteSessionConfig,
    RemoteShellContext,
    ShellCommandRequest,
    ShellCommandResult,
)
from opencompany.sandbox.base import SandboxBackend, SandboxError
from opencompany.status_machine import AGENT_TERMINAL_STATUSES, KNOWN_AGENT_STATUSES
from opencompany.storage import Storage
from opencompany.tools.runtime import (
    decode_offset_cursor,
    encode_offset_cursor,
    normalize_tool_run_limit,
)
from opencompany.utils import (
    json_ready,
    resolve_in_workspace,
    stable_json_dumps,
    truncate_text,
    utc_now,
)
from opencompany.workspace import WorkspaceManager


class ToolExecutionError(ValueError):
    pass


class InvalidActionArgumentsError(ToolExecutionError):
    def __init__(
        self,
        *,
        action_type: str,
        expected_keys: tuple[str, ...],
        provided_keys: tuple[str, ...],
    ) -> None:
        self.action_type = action_type
        self.expected_keys = expected_keys
        self.provided_keys = provided_keys
        joined = " or ".join(f"'{key}'" for key in expected_keys)
        super().__init__(f"Action '{action_type}' requires {joined}.")


AgentEventLogger = Callable[..., None]
DiagnosticLoggerFn = Callable[..., None]
WorkerInitialMessageFactory = Callable[[str, Path], str]
AgentIdFactory = Callable[[], str]

GET_AGENT_RUN_MAX_MESSAGES = 5
GET_AGENT_RUN_VISIBLE_MESSAGE_FIELDS = (
    "content",
    "reasoning",
    "role",
    "tool_calls",
    "tool_call_id",
)
KNOWN_AGENT_RUN_STATUS_SET = frozenset(KNOWN_AGENT_STATUSES)


class ToolExecutor:
    def __init__(
        self,
        *,
        app_dir: Path,
        project_dir: Path,
        config: OpenCompanyConfig,
        storage: Storage,
        sandbox_backend_cls: type[SandboxBackend],
        log_agent_event: AgentEventLogger,
        log_diagnostic: DiagnosticLoggerFn,
    ) -> None:
        self.app_dir = app_dir
        self.project_dir = project_dir
        self.config = config
        self.storage = storage
        self.sandbox_backend_cls = sandbox_backend_cls
        self._log_agent_event = log_agent_event
        self._log_diagnostic = log_diagnostic
        self._shell_backend_instance: SandboxBackend | None = None
        self._session_remote_context: dict[str, RemoteShellContext] = {}

    def set_project_dir(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def set_session_remote_config(
        self,
        session_id: str,
        config: RemoteSessionConfig | None,
        *,
        password: str | None = None,
    ) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        if config is None:
            self._session_remote_context.pop(normalized_session_id, None)
            return
        self._session_remote_context[normalized_session_id] = RemoteShellContext(
            session_id=normalized_session_id,
            config=config,
            password=str(password or ""),
        )

    def session_remote_context(self, session_id: str) -> RemoteShellContext | None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        return self._session_remote_context.get(normalized_session_id)

    def clear_session_remote_password(self, session_id: str) -> None:
        context = self.session_remote_context(session_id)
        if context is None:
            return
        context.password = ""

    def clear_session_remote_context(self, session_id: str) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        self._session_remote_context.pop(normalized_session_id, None)

    def cleanup_session_remote_runtime(self, session_id: str) -> None:
        backend = self._shell_backend_instance
        if backend is not None:
            backend.cleanup_session(str(session_id or "").strip())
        self.clear_session_remote_context(session_id)

    def _shell_backend(self) -> SandboxBackend:
        backend = self._shell_backend_instance
        if backend is None:
            backend = self.sandbox_backend_cls(self.config.sandbox, self.app_dir)
            self._shell_backend_instance = backend
        return backend

    def shell_backend(self) -> SandboxBackend:
        return self._shell_backend()

    def timeout_seconds_for_action(self, action_type: str) -> float:
        return self.config.runtime.tool_timeouts.seconds_for(
            action_type,
            shell_fallback_seconds=float(self.config.sandbox.timeout_seconds),
        )

    @staticmethod
    def _unique_agent_name(base_name: str, agents: dict[str, AgentNode]) -> str:
        normalized_base = str(base_name or "").strip() or "Worker Agent"
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

    def build_shell_request(
        self,
        *,
        workspace_root: Path,
        command: str,
        cwd: str = ".",
        writable_paths: list[Path] | None = None,
        environment: dict[str, str] | None = None,
        session_id: str = "",
        remote: RemoteShellContext | None = None,
    ) -> ShellCommandRequest:
        normalized_command = str(command or "").strip()
        if not normalized_command:
            raise ValueError("Shell command is required.")
        if remote is not None:
            root = self._normalize_remote_workspace_root(str(workspace_root))
            resolved_cwd = Path(
                self._resolve_remote_workspace_path(root, str(cwd or ".")).as_posix()
            )
            requested_writable_paths = list(writable_paths or [Path(root.as_posix())])
            normalized_writable_paths: list[Path] = []
            for path in requested_writable_paths:
                resolved_path = self._resolve_remote_workspace_path(root, str(path))
                normalized_writable_paths.append(Path(resolved_path.as_posix()))
            return ShellCommandRequest(
                command=normalized_command,
                cwd=resolved_cwd,
                workspace_root=Path(root.as_posix()),
                writable_paths=normalized_writable_paths,
                timeout_seconds=self.timeout_seconds_for_action("shell"),
                network_policy=self.config.sandbox.network_policy,
                allowed_domains=list(self.config.sandbox.allowed_domains),
                environment=dict(environment or {}),
                session_id=str(session_id or ""),
                remote=remote,
            )
        root = workspace_root.resolve()
        relative_cwd = self.normalize_workspace_path(root, cwd or ".")
        resolved_cwd = resolve_in_workspace(root, relative_cwd)
        requested_writable_paths = list(writable_paths or [root])
        normalized_writable_paths: list[Path] = []
        for path in requested_writable_paths:
            resolved_path = Path(path).expanduser().resolve()
            if resolved_path != root and root not in resolved_path.parents:
                raise ValueError(f"Writable path escapes workspace: {resolved_path}")
            normalized_writable_paths.append(resolved_path)
        return ShellCommandRequest(
            command=normalized_command,
            cwd=resolved_cwd,
            workspace_root=root,
            writable_paths=normalized_writable_paths,
            timeout_seconds=self.timeout_seconds_for_action("shell"),
            network_policy=self.config.sandbox.network_policy,
            allowed_domains=list(self.config.sandbox.allowed_domains),
            environment=dict(environment or {}),
            session_id=str(session_id or ""),
            remote=remote,
        )

    @staticmethod
    def _normalize_remote_workspace_root(raw_path: str) -> PurePosixPath:
        normalized = str(raw_path or "").strip()
        if not normalized:
            raise ValueError("Remote workspace root is required.")
        root = PurePosixPath(posixpath.normpath(normalized))
        if not root.is_absolute():
            raise ValueError("Remote workspace root must be an absolute POSIX path.")
        return root

    @staticmethod
    def _resolve_remote_workspace_path(
        workspace_root: PurePosixPath,
        requested_path: str,
    ) -> PurePosixPath:
        normalized = str(requested_path or ".").strip() or "."
        if normalized.startswith("~"):
            raise ValueError("Remote path must not use '~'. Use an absolute path instead.")
        target = PurePosixPath(normalized)
        if target.is_absolute():
            candidate = PurePosixPath(posixpath.normpath(target.as_posix()))
        else:
            candidate = PurePosixPath(
                posixpath.normpath((workspace_root / target).as_posix())
            )
        if not candidate.is_absolute():
            candidate = PurePosixPath("/") / candidate
        if candidate != workspace_root and workspace_root not in candidate.parents:
            raise ValueError(f"Path escapes workspace: {normalized}")
        return candidate

    def execute_read_only(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        tool_run_id: str | None = None,
    ) -> dict[str, Any]:
        action_type = action["type"]
        normalized_tool_run_id = str(tool_run_id or "").strip()
        self._log_agent_event(
            agent,
            event_type="tool_call_started",
            phase="tool",
            payload={
                "action": json_ready(_public_action(action)),
                **({"tool_run_id": normalized_tool_run_id} if normalized_tool_run_id else {}),
            },
        )
        result = self._execute_read_only_result(
            agent=agent,
            action=action,
            agents=agents,
            workspace_manager=workspace_manager,
        )
        self._log_agent_event(
            agent,
            event_type="tool_call",
            phase="tool",
            payload={
                "action": json_ready(_public_action(action)),
                "result": json_ready(result),
                "result_preview": truncate_text(stable_json_dumps(result), 1200),
                **({"tool_run_id": normalized_tool_run_id} if normalized_tool_run_id else {}),
            },
        )
        return result

    async def execute_shell(
        self,
        agent: AgentNode,
        action: dict[str, Any],
        workspace_manager: WorkspaceManager,
        *,
        stream_listener: Callable[[str, str], Any] | None = None,
        tool_run_id: str | None = None,
    ) -> dict[str, Any]:
        workspace = workspace_manager.workspace(agent.workspace_id)
        normalized_tool_run_id = str(tool_run_id or "").strip()
        try:
            command = self.require_action_string(action, "command")
        except InvalidActionArgumentsError as exc:
            result = self._invalid_arguments_result(agent=agent, action=action, error=exc)
            result["next_step_hint"] = (
                "Provide a non-empty 'command' and keep writes inside the assigned workspace."
            )
            self._log_agent_event(
                agent,
                event_type="tool_call",
                phase="shell",
                payload={
                    "action": json_ready(_public_action(action)),
                    "result": result,
                    **({"tool_run_id": normalized_tool_run_id} if normalized_tool_run_id else {}),
                },
            )
            return result
        try:
            remote_context = self.session_remote_context(agent.session_id)
            if (
                remote_context is not None
                and remote_context.config.auth_mode == "password"
                and not str(remote_context.password or "").strip()
            ):
                raise ValueError(
                    "Remote session requires password auth. Provide remote_password when starting or resuming the session."
                )
            workspace_root = (
                Path(remote_context.config.remote_dir)
                if remote_context is not None
                else workspace.path
            )
            request = self.build_shell_request(
                workspace_root=workspace_root,
                command=command,
                cwd=str(action.get("cwd", ".")),
                session_id=agent.session_id,
                remote=remote_context,
            )
        except ValueError as exc:
            self._log_agent_event(
                agent,
                event_type="sandbox_violation",
                phase="tool",
                payload={
                    "error": str(exc),
                    "command": command,
                    "cwd": str(action.get("cwd", ".")),
                },
            )
            return {
                "error": str(exc),
                "command": command,
                "cwd": str(action.get("cwd", ".")),
            }
        cwd = request.cwd
        backend = self._shell_backend()
        self._log_diagnostic(
            "shell_network_policy_applied",
            session_id=agent.session_id,
            agent_id=agent.id,
            payload={
                "network_policy": request.network_policy,
                "allowed_domain_count": len(request.allowed_domains),
                "cwd": str(cwd),
            },
        )
        self._log_agent_event(
            agent,
            event_type="tool_call_started",
            phase="shell",
            payload={
                "action": json_ready(_public_action_for_stream(action)),
                "cwd": str(cwd),
                **({"tool_run_id": normalized_tool_run_id} if normalized_tool_run_id else {}),
            },
        )

        async def on_event(channel: str, text: str) -> None:
            self._log_agent_event(
                agent,
                event_type="shell_stream",
                phase=channel,
                payload={"text": text},
            )
            if stream_listener is None:
                return
            try:
                maybe_listener = stream_listener(channel, text)
                if asyncio.iscoroutine(maybe_listener):
                    await maybe_listener
            except Exception as exc:
                self._log_diagnostic(
                    "shell_stream_listener_failed",
                    level="warning",
                    session_id=agent.session_id,
                    agent_id=agent.id,
                    payload={
                        "channel": channel,
                        "listener_type": type(stream_listener).__name__,
                    },
                    error=exc,
                )
        try:
            result = await backend.run_command(request, on_event=on_event)
        except SandboxError as exc:
            self._log_agent_event(
                agent,
                event_type="sandbox_violation",
                phase="tool",
                payload={"error": str(exc), "command": command},
            )
            return {"error": str(exc), "command": command}
        result_payload = json_ready(asdict(result))
        if result.timed_out:
            self._log_agent_event(
                agent,
                event_type="shell_timeout",
                phase="shell",
                payload={
                    "action": json_ready(_public_action(action)),
                    "cwd": str(cwd),
                    "result": result_payload,
                },
            )
            self._log_diagnostic(
                "shell_command_timed_out",
                level="warning",
                session_id=agent.session_id,
                agent_id=agent.id,
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "timeout_seconds": result.timeout_seconds,
                    "duration_ms": result.duration_ms,
                    "termination_reason": result.termination_reason,
                    "reader_tasks_cancelled": result.reader_tasks_cancelled,
                    "stdout_preview": truncate_text(result.stdout, 1200),
                    "stderr_preview": truncate_text(result.stderr, 1200),
                },
            )
        elif result.reader_tasks_cancelled:
            self._log_agent_event(
                agent,
                event_type="shell_stream_drain_timeout",
                phase="shell",
                payload={
                    "action": json_ready(_public_action(action)),
                    "cwd": str(cwd),
                    "result": result_payload,
                },
            )
            self._log_diagnostic(
                "shell_stream_drain_timed_out",
                level="warning",
                session_id=agent.session_id,
                agent_id=agent.id,
                payload={
                    "command": command,
                    "cwd": str(cwd),
                    "duration_ms": result.duration_ms,
                    "stdout_preview": truncate_text(result.stdout, 1200),
                    "stderr_preview": truncate_text(result.stderr, 1200),
                },
            )
        permission_error = _shell_result_sandbox_violation(result)
        if permission_error is not None:
            self._log_agent_event(
                agent,
                event_type="sandbox_violation",
                phase="shell",
                payload={
                    "error": permission_error,
                    "command": command,
                    "cwd": str(cwd),
                    "exit_code": result.exit_code,
                    "stderr": truncate_text(result.stderr, 400),
                },
            )
        self._log_agent_event(
            agent,
            event_type="tool_call",
            phase="shell",
            payload={
                "action": json_ready(_public_action_for_stream(action)),
                "result": _shell_result_for_stream(action, result_payload),
                **({"tool_run_id": normalized_tool_run_id} if normalized_tool_run_id else {}),
            },
        )
        return result_payload

    def spawn_child(
        self,
        *,
        parent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
        new_agent_id: AgentIdFactory,
        worker_initial_message: WorkerInitialMessageFactory,
    ) -> str | None:
        try:
            instruction = self.require_action_string(action, "instruction")
        except ToolExecutionError as exc:
            self._log_diagnostic(
                "child_spawn_skipped",
                level="warning",
                session_id=parent.session_id,
                agent_id=parent.id,
                payload={
                    "reason": "invalid_action",
                    "error": str(exc),
                    "requested_name": str(action.get("name", "Worker Agent")),
                },
            )
            return None
        normalized_instruction = instruction.strip()
        duplicate_child = next(
            (
                agents[child_id]
                for child_id in parent.children
                if (child := agents.get(child_id))
                and child.status not in AGENT_TERMINAL_STATUSES
                and child.instruction.strip() == normalized_instruction
            ),
            None,
        )
        if duplicate_child is not None:
            self._log_diagnostic(
                "child_spawn_skipped",
                level="warning",
                session_id=parent.session_id,
                agent_id=parent.id,
                payload={
                    "reason": "duplicate_child_instruction",
                    "requested_name": str(action.get("name", "Worker Agent")),
                    "requested_instruction": instruction,
                    "existing_child_id": duplicate_child.id,
                    "existing_child_status": duplicate_child.status.value,
                },
            )
            return None
        if len(parent.children) >= self.config.runtime.limits.max_children_per_agent:
            limit_details = child_limit_details(
                parent,
                agents,
                self.config.runtime.limits.max_children_per_agent,
            )
            self._log_diagnostic(
                "child_spawn_skipped",
                level="warning",
                session_id=parent.session_id,
                agent_id=parent.id,
                payload={
                    "reason": "max_children_per_agent",
                    "requested_name": str(action.get("name", "Worker Agent")),
                    "detail": (
                        "Per-agent child fan-out is capped across the whole session; "
                        "terminal children still count toward this limit."
                    ),
                    **limit_details,
                },
            )
            return None
        agent_id = new_agent_id()
        while agent_id in agents:
            agent_id = new_agent_id()
        root_workspace = workspace_manager.root_workspace()
        if (
            root_workspace is not None
            and root_workspace.path.resolve() == self.project_dir.resolve()
        ):
            workspace = root_workspace
        else:
            workspace = workspace_manager.fork_workspace(parent.workspace_id, agent_id)
        child = AgentNode(
            id=agent_id,
            session_id=parent.session_id,
            name=self._unique_agent_name(str(action.get("name", "Worker Agent")), agents),
            role=AgentRole.WORKER,
            instruction=instruction,
            workspace_id=workspace.id,
            parent_agent_id=parent.id,
            metadata={
                "created_at": utc_now(),
                "model": self.config.llm.openrouter.model_for_role(AgentRole.WORKER.value),
                **(
                    {"skills_catalog": json_ready(parent.metadata.get("skills_catalog"))}
                    if isinstance(parent.metadata, dict)
                    and isinstance(parent.metadata.get("skills_catalog"), dict)
                    else {}
                ),
            },
        )
        child.conversation = [
            {
                "role": "user",
                "content": worker_initial_message(child.instruction, workspace.path),
            }
        ]
        parent.children.append(child.id)
        agents[child.id] = child
        self.storage.upsert_agent(parent)
        self.storage.upsert_agent(child)
        self._log_diagnostic(
            "child_spawned",
            session_id=child.session_id,
            agent_id=child.id,
            payload={
                "parent_agent_id": parent.id,
                "workspace_id": workspace.id,
                "workspace_path": str(workspace.path),
            },
        )
        self._log_agent_event(
            child,
            event_type="agent_spawned",
            phase="scheduler",
            payload={
                "instruction": child.instruction,
                "workspace": str(workspace.path),
            },
        )
        return child.id

    def _execute_read_only_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        agents: dict[str, AgentNode],
        workspace_manager: WorkspaceManager,
    ) -> dict[str, Any]:
        action_type = str(action.get("type", "")).strip()
        del workspace_manager
        if not action_type:
            return self._invalid_action_result(
                agent=agent,
                action=action,
                error="Action is missing a non-empty 'type'.",
                error_code="invalid_action",
                next_step_hint=(
                    "Choose one supported tool and include its required JSON arguments."
                ),
            )
        try:
            if action_type == "list_agent_runs":
                limit = self._normalized_page_limit(action)
                offset = self._decoded_offset(action)
                if offset is None:
                    return {"error": "list_agent_runs received an invalid 'cursor'."}
                statuses, invalid_statuses = _normalized_agent_statuses(action.get("status"))
                if invalid_statuses:
                    invalid = ", ".join(f"'{item}'" for item in invalid_statuses)
                    allowed = ", ".join(sorted(KNOWN_AGENT_RUN_STATUS_SET))
                    return {
                        "error": (
                            f"list_agent_runs received invalid status filter(s): {invalid}. "
                            f"Allowed: {allowed}."
                        )
                    }
                ordered_agents = sorted(
                    agents.values(),
                    key=_agent_list_sort_key,
                    reverse=True,
                )
                rows: list[dict[str, Any]] = []
                for other in ordered_agents:
                    status = public_agent_status(other)
                    if statuses and status.lower() not in statuses:
                        continue
                    rows.append(
                        {
                            "id": other.id,
                            "name": other.name,
                            "role": other.role.value,
                            "status": status,
                            "created_at": _agent_created_at(other),
                            "summary_short": truncate_text(str(other.summary or ""), 160).strip(),
                            "messages_count": len(other.conversation),
                        }
                    )
                page, next_cursor, has_more = self._paginate_items(
                    rows,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "agent_runs_count": len(page),
                    "agent_runs": page,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                }
            if action_type == "get_agent_run":
                target_id = self.require_action_string(action, "agent_id")
                if self._id_kind(target_id) == "tool_run_id":
                    return {
                        "error": (
                            "get_agent_run expects 'agent_id' (prefix 'agent-'), "
                            f"but received tool_run_id '{target_id}'."
                        )
                    }
                target = agents.get(target_id)
                if target is None:
                    return {"error": f"Agent {target_id} was not found."}
                excluded_indices = _agent_run_excluded_message_indices(target)
                messages = [
                    dict(item)
                    for index, item in enumerate(target.conversation)
                    if isinstance(item, dict) and index not in excluded_indices
                ]
                messages_count = len(messages)
                start_raw = action.get("messages_start")
                end_raw = action.get("messages_end")
                if start_raw is None and end_raw is None:
                    start = max(0, messages_count - 1)
                    end = messages_count
                else:
                    lower_bound, upper_bound = _relative_slice_bounds(messages_count)
                    raw_start_index = _safe_int(start_raw, default=0)
                    if raw_start_index is None:
                        return {
                            "error": (
                                "get_agent_run field 'messages_start' must be an integer "
                                "(supports negative indexes like -1)."
                            )
                        }
                    if raw_start_index < lower_bound or raw_start_index > upper_bound:
                        return {
                            "error": (
                                "get_agent_run field 'messages_start' is out of range for "
                                f"{messages_count} messages: received {raw_start_index}; "
                                f"allowed range is [{lower_bound}, {upper_bound}]."
                            )
                        }
                    raw_end_index = _safe_int(end_raw, default=messages_count)
                    if raw_end_index is None:
                        return {
                            "error": (
                                "get_agent_run field 'messages_end' must be an integer "
                                "(supports negative indexes like -1)."
                            )
                        }
                    if raw_end_index < lower_bound or raw_end_index > upper_bound:
                        return {
                            "error": (
                                "get_agent_run field 'messages_end' is out of range for "
                                f"{messages_count} messages: received {raw_end_index}; "
                                f"allowed range is [{lower_bound}, {upper_bound}]."
                            )
                        }
                    start = _resolve_relative_slice_index(raw_start_index, size=messages_count)
                    end = _resolve_relative_slice_index(raw_end_index, size=messages_count)
                    if end < start:
                        return {
                            "error": (
                                "get_agent_run received invalid [messages_start,messages_end) "
                                f"indices after normalization: start={start}, end={end}. "
                                "'messages_end' must be >= 'messages_start'."
                            )
                        }
                requested_end = end
                end = min(end, start + GET_AGENT_RUN_MAX_MESSAGES)
                sliced = [_project_agent_run_message(item) for item in messages[start:end]]
                result: dict[str, Any] = {
                    "agent_run": {
                        "id": target.id,
                        "name": target.name,
                        "role": target.role.value,
                        "status": public_agent_status(target),
                        "created_at": _agent_created_at(target),
                        "parent_agent_id": target.parent_agent_id,
                        "children_count": len(target.children),
                        "step_count": int(target.step_count),
                    },
                    "messages": sliced,
                }
                if requested_end > end:
                    result["warning"] = (
                        "get_agent_run returned only the first 5 messages for the requested "
                        "slice; call again with messages_start=next_messages_start to continue."
                    )
                    result["next_messages_start"] = end
                return result
            if action_type == "cancel_agent":
                target_id = self.require_action_string(action, "agent_id")
                if str(target_id).strip().lower().startswith("toolrun-"):
                    return {
                        "error": (
                            "cancel_agent expects 'agent_id' (prefix 'agent-'), "
                            f"but received tool_run_id '{target_id}'."
                        )
                    }
                if target_id == agent.id:
                    return {
                        "cancel_agent_status": False,
                        "error": "cancel_agent cannot target the current agent itself.",
                    }
                target = agents.get(target_id)
                if target is None or not is_descendant(agent.id, target_id, agents):
                    return {
                        "cancel_agent_status": False,
                        "error": f"Cannot cancel agent {target_id}.",
                    }
                recursive = _coerce_bool(action.get("recursive"), default=True)
                target_ids = (
                    _descendant_tree_ids(root_id=target_id, agents=agents)
                    if recursive
                    else [target_id]
                )
                for target_item_id in target_ids:
                    node = agents.get(target_item_id)
                    if node is None:
                        continue
                    if node.status in AGENT_TERMINAL_STATUSES:
                        continue
                    node.status = AgentStatus.CANCELLED
                    node.status_reason = f"cancel_source:agent:{agent.id}"
                    node.completion_status = "cancelled"
                    if not str(node.summary or "").strip():
                        node.summary = "Agent cancelled."
                    self.storage.upsert_agent(node)
                return {
                    "cancel_agent_status": True,
                }
            return self._unknown_tool_result(agent=agent, action=action)
        except InvalidActionArgumentsError as exc:
            return self._invalid_arguments_result(agent=agent, action=action, error=exc)
        except ValueError as exc:
            return self._invalid_action_result(
                agent=agent,
                action=action,
                error=str(exc),
                error_code="invalid_arguments",
            )

    def normalize_workspace_path(self, workspace_root: Path, requested_path: str) -> str:
        path = Path(requested_path)
        if not path.is_absolute():
            return requested_path
        try:
            return str(path.resolve().relative_to(workspace_root.resolve())) or "."
        except ValueError:
            pass
        try:
            return str(path.resolve().relative_to(self.project_dir.resolve())) or "."
        except ValueError:
            return requested_path

    @staticmethod
    def require_action_string(action: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = action.get(key)
            if value is None:
                continue
            text = str(value)
            if text:
                return text
        raise InvalidActionArgumentsError(
            action_type=str(action.get("type", "<unknown>")),
            expected_keys=tuple(keys),
            provided_keys=tuple(
                sorted(
                    key
                    for key in action.keys()
                    if key not in {"type", "_tool_call_id"}
                )
            ),
        )

    @staticmethod
    def _id_kind(identifier: str) -> str:
        normalized = identifier.strip().lower()
        if normalized.startswith("toolrun-"):
            return "tool_run_id"
        if normalized.startswith("agent-"):
            return "agent_id"
        return "unknown"

    def _role_tool_names(self, role: AgentRole) -> tuple[str, ...]:
        return tuple(self.config.runtime.tools.tool_names_for_role(role.value))

    def _unknown_tool_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        action_type = str(action.get("type", "<unknown>")).strip() or "<unknown>"
        available_tools = list(self._role_tool_names(agent.role))
        suggestions = get_close_matches(action_type, available_tools, n=3, cutoff=0.45)
        if action_type in {"write_file", "edit_file", "append_file", "create_file"} and "shell" in available_tools:
            next_step_hint = (
                "This runtime does not expose write_file. Use shell to edit files inside the workspace "
                "(for example: cat > file <<'EOF' ...)."
            )
        elif suggestions:
            next_step_hint = (
                "Tool name is not available here. Try one of: "
                + ", ".join(f"'{name}'" for name in suggestions)
                + "."
            )
        else:
            next_step_hint = (
                "Choose one of the available tools and retry with valid JSON arguments."
            )
        return self._invalid_action_result(
            agent=agent,
            action=action,
            error=(
                f"Tool '{action_type}' is not available for {agent.role.value} agents in this runtime."
            ),
            error_code="unknown_tool",
            next_step_hint=next_step_hint,
            include_suggestions=suggestions,
        )

    def _invalid_arguments_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        error: InvalidActionArgumentsError,
    ) -> dict[str, Any]:
        expected = [list(error.expected_keys)] if error.expected_keys else []
        provided = list(error.provided_keys)
        action_type = str(action.get("type", "<unknown>"))
        next_step_hint = (
            f"Call '{action_type}' again with a non-empty "
            + " or ".join(f"'{key}'" for key in error.expected_keys)
            + "."
        )
        return self._invalid_action_result(
            agent=agent,
            action=action,
            error=str(error),
            error_code="invalid_arguments",
            next_step_hint=next_step_hint,
            expected_arguments=expected,
            provided_arguments=provided,
        )

    def _invalid_action_result(
        self,
        *,
        agent: AgentNode,
        action: dict[str, Any],
        error: str,
        error_code: str,
        next_step_hint: str | None = None,
        include_suggestions: list[str] | None = None,
        expected_arguments: list[list[str]] | None = None,
        provided_arguments: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": error,
            "error_code": error_code,
            "action": self._action_feedback_payload(action),
            "available_tools": list(self._role_tool_names(agent.role)),
        }
        if include_suggestions:
            payload["suggested_tools"] = include_suggestions
        if expected_arguments:
            payload["expected_arguments"] = expected_arguments
        if provided_arguments is not None:
            payload["provided_arguments"] = provided_arguments
        if next_step_hint:
            payload["next_step_hint"] = next_step_hint
        return payload

    @staticmethod
    def _action_feedback_payload(action: dict[str, Any]) -> dict[str, Any]:
        public = _public_action(action)
        preview: dict[str, Any] = {}
        for key, value in public.items():
            if isinstance(value, str):
                preview[key] = truncate_text(value, 200)
                continue
            if isinstance(value, (bool, int, float)) or value is None:
                preview[key] = value
                continue
            preview[key] = truncate_text(stable_json_dumps(value), 200)
        return preview

    def _normalized_page_limit(self, action: dict[str, Any]) -> int:
        default_limit, max_limit = self.config.runtime.tools.list_limit_bounds()
        return normalize_tool_run_limit(
            action.get("limit", default_limit),
            default=default_limit,
            minimum=1,
            maximum=max_limit,
        )

    @staticmethod
    def _decoded_offset(action: dict[str, Any]) -> int | None:
        raw = action.get("cursor")
        text = str(raw).strip() if raw is not None else None
        return decode_offset_cursor(text, default=0)

    @staticmethod
    def _paginate_items(
        items: list[Any],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[Any], str | None, bool]:
        start = max(0, int(offset))
        stop = start + max(1, int(limit))
        page = items[start:stop]
        has_more = stop < len(items)
        next_cursor = encode_offset_cursor(stop) if has_more else None
        return page, next_cursor, has_more


def child_summaries(parent: AgentNode, agents: dict[str, AgentNode]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for child_id in parent.children:
        child = agents.get(child_id)
        if not child or child.status not in {
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.TERMINATED,
        }:
            continue
        summaries.append(
            {
                "id": child.id,
                "name": child.name,
                "status": public_agent_status(child),
                "summary": child.summary,
                "next_recommendation": child.next_recommendation,
            }
        )
    return summaries


def public_agent_status(agent: AgentNode) -> str:
    return agent.status.value


def _shell_result_sandbox_violation(result: ShellCommandResult) -> str | None:
    if result.exit_code == 0:
        return None
    combined = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
    if not combined:
        return None
    normalized = combined.lower()
    if not any(
        marker in normalized
        for marker in ("operation not permitted", "permission denied", "read-only file system")
    ):
        return None
    return "Shell command could not write outside the sandbox: " + truncate_text(combined, 400)


def child_limit_details(
    parent: AgentNode,
    agents: dict[str, AgentNode],
    limit: int,
) -> dict[str, Any]:
    active_statuses = {
        AgentStatus.PENDING,
        AgentStatus.RUNNING,
    }
    active_children = sum(
        1
        for child_id in parent.children
        if (child := agents.get(child_id)) and child.status in active_statuses
    )
    return {
        "current_children": len(parent.children),
        "active_children": active_children,
        "limit": limit,
        "limit_scope": "total_children",
    }


def is_descendant(
    ancestor_id: str,
    candidate_id: str,
    agents: dict[str, AgentNode],
) -> bool:
    current = agents.get(candidate_id)
    while current:
        if current.parent_agent_id == ancestor_id:
            return True
        current = agents.get(current.parent_agent_id) if current.parent_agent_id else None
    return False


def _public_action(action: dict[str, Any]) -> dict[str, Any]:
    return dict(action)


def _public_action_for_stream(action: dict[str, Any]) -> dict[str, Any]:
    return _public_action(action)


def _project_agent_run_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        key: message[key]
        for key in GET_AGENT_RUN_VISIBLE_MESSAGE_FIELDS
        if key in message
    }


def _agent_run_excluded_message_indices(agent: AgentNode) -> set[int]:
    raw_indices = agent.metadata.get("compression_excluded_message_indices")
    if not isinstance(raw_indices, list):
        return set()
    normalized: set[int] = set()
    for value in raw_indices:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index >= 0:
            normalized.add(index)
    return normalized


def _shell_result_for_stream(
    action: dict[str, Any],
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    if str(action.get("type", "")) != "shell":
        return result_payload
    sanitized = dict(result_payload)
    sanitized.pop("command", None)
    return sanitized


def _agent_created_at(agent: AgentNode) -> str:
    raw = agent.metadata.get("created_at")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _agent_list_sort_key(agent: AgentNode) -> tuple[float, str, str]:
    created_at = _agent_created_at(agent)
    return (_iso8601_to_epoch(created_at), created_at, agent.id)


def _iso8601_to_epoch(value: str) -> float:
    if not value:
        return float("-inf")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed.timestamp()


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


def _safe_int(value: Any, *, default: int) -> int | None:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relative_slice_bounds(size: int) -> tuple[int, int]:
    if size <= 0:
        return 0, 0
    return -size, size


def _resolve_relative_slice_index(index: int, *, size: int) -> int:
    if index < 0:
        return size + index
    return index


def _normalized_agent_statuses(raw_status: Any) -> tuple[set[str] | None, list[str]]:
    normalized_items: set[str] = set()
    if isinstance(raw_status, str):
        item = raw_status.strip().lower()
        if item:
            normalized_items.add(item)
    elif isinstance(raw_status, (list, tuple, set)):
        normalized_items = {
            str(item).strip().lower()
            for item in raw_status
            if str(item).strip()
        }
    if not normalized_items:
        return None, []
    valid = {item for item in normalized_items if item in KNOWN_AGENT_RUN_STATUS_SET}
    invalid = sorted(item for item in normalized_items if item not in KNOWN_AGENT_RUN_STATUS_SET)
    return (valid or None), invalid


def _descendant_tree_ids(
    *,
    root_id: str,
    agents: dict[str, AgentNode],
) -> list[str]:
    ordered: list[str] = []
    stack = [root_id]
    visited: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        ordered.append(current_id)
        node = agents.get(current_id)
        if node is None:
            continue
        stack.extend(reversed(node.children))
    return ordered
