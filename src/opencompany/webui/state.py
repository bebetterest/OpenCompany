from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencompany.config import OpenCompanyConfig
from opencompany.i18n import TRANSLATIONS, Translator
from opencompany.models import (
    RemoteSessionConfig,
    RunSession,
    WorkspaceMode,
    normalize_workspace_mode,
)
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.paths import RuntimePaths
from opencompany.remote import (
    load_remote_session_config,
    load_remote_session_password,
    normalize_remote_session_config,
)
from opencompany.sandbox.registry import available_sandbox_backends, resolve_sandbox_backend_cls
from opencompany.skills import normalize_skill_ids

from .events import EventHub


@dataclass(slots=True)
class SessionLaunchConfig:
    project_dir: Path | None
    session_id: str | None
    session_mode: WorkspaceMode
    session_mode_locked: bool
    remote: dict[str, Any] | None
    sandbox_backend: str
    sandbox_backend_default: str
    sandbox_backends: tuple[str, ...]

    @classmethod
    def create(
        cls,
        project_dir: Path | None,
        session_id: str | None,
        *,
        session_mode: WorkspaceMode | str | None = None,
        session_mode_locked: bool = False,
        remote: dict[str, Any] | None = None,
        sandbox_backend: str = "anthropic",
        sandbox_backend_default: str = "anthropic",
        sandbox_backends: tuple[str, ...] = ("anthropic", "none"),
    ) -> SessionLaunchConfig:
        normalized_project = project_dir.resolve() if project_dir else None
        normalized_session_text = (session_id or "").strip()
        normalized_session = (
            RuntimePaths.normalize_session_id(normalized_session_text)
            if normalized_session_text
            else None
        )
        return cls(
            project_dir=normalized_project,
            session_id=normalized_session,
            session_mode=normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value),
            session_mode_locked=bool(session_mode_locked),
            remote=remote if isinstance(remote, dict) else None,
            sandbox_backend=str(sandbox_backend or "anthropic").strip().lower() or "anthropic",
            sandbox_backend_default=(
                str(sandbox_backend_default or "anthropic").strip().lower() or "anthropic"
            ),
            sandbox_backends=tuple(str(item).strip().lower() for item in sandbox_backends if str(item).strip()),
        )

    def can_run(self) -> bool:
        has_local = self.project_dir is not None and self.project_dir.is_dir()
        has_remote = (
            isinstance(self.remote, dict)
            and bool(str(self.remote.get("ssh_target", "")).strip())
            and bool(str(self.remote.get("remote_dir", "")).strip())
        )
        return bool(self.session_id) or has_local or has_remote

    def can_resume(self) -> bool:
        return bool(self.session_id)


class WebUIRuntimeState:
    def __init__(
        self,
        *,
        project_dir: Path | None,
        session_id: str | None,
        remote: dict[str, Any] | RemoteSessionConfig | None = None,
        remote_password: str | None = None,
        app_dir: Path | None,
        locale: str | None,
        debug: bool,
        session_mode: WorkspaceMode | str | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve() if project_dir else None
        self.configured_resume_session_id = self._normalize_optional_session_id(session_id)
        self.app_dir = app_dir.resolve() if app_dir else None
        self.orchestrator: Orchestrator | None = None
        self.locale_override: str | None = locale if locale in {"en", "zh"} else None
        self.locale = self._resolve_configured_locale(self.locale_override)
        self.translator = Translator(self.locale)
        self.debug_enabled = bool(debug)
        self.session_mode = normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value)
        self.remote_config: RemoteSessionConfig | None = (
            normalize_remote_session_config(remote)
            if isinstance(remote, (dict, RemoteSessionConfig)) and remote
            else None
        )
        if self.remote_config is not None and self.session_mode != WorkspaceMode.DIRECT:
            raise ValueError("Remote workspace is supported only in direct mode.")
        if self.remote_config is not None:
            self.project_dir = None
        self.remote_password: str = str(remote_password or "").strip()
        self.session_mode_locked = False
        self._sandbox_backends = self._available_sandbox_backends()
        self.sandbox_backend_default = self._default_sandbox_backend_from_config()
        self.sandbox_backend = self._normalize_sandbox_backend(self.sandbox_backend_default)

        self.session_task: asyncio.Task[None] | None = None
        self.current_session_id: str | None = None
        self.current_task: str = ""
        self.current_session_status: str = "idle"
        self.current_summary: str = ""
        self.status_message: str = self.translator.text("ready")
        self.selected_model: str = self._default_model_from_config()
        self.keep_pinned_messages: int = self._default_keep_pinned_messages_from_config()
        self.root_agent_name: str = ""
        self.selected_skill_ids: list[str] = []
        self.skills_state: dict[str, Any] = {}
        self.available_skills: list[dict[str, Any]] = []
        self.selected_mcp_server_ids: list[str] = []
        self.mcp_state: dict[str, Any] = {}
        self.available_mcp_servers: list[dict[str, Any]] = []
        self.project_sync_action_in_progress: bool = False

        self.event_hub = EventHub()
        self._state_lock = asyncio.Lock()
        if self.configured_resume_session_id:
            with suppress(Exception):
                self._load_configured_session_context(self.configured_resume_session_id)

    def launch_config(self) -> SessionLaunchConfig:
        return SessionLaunchConfig.create(
            self.project_dir,
            self.configured_resume_session_id,
            session_mode=self.session_mode,
            session_mode_locked=self.session_mode_locked,
            remote=self._remote_config_payload(),
            sandbox_backend=self.sandbox_backend,
            sandbox_backend_default=self.sandbox_backend_default,
            sandbox_backends=self._sandbox_backends,
        )

    def snapshot(self) -> dict[str, Any]:
        self.refresh_runtime_config()
        config = self.launch_config()
        project_dir_display = self._project_dir_display()
        return {
            "locale": self.locale,
            "translations": TRANSLATIONS.get(self.locale, TRANSLATIONS["en"]),
            "launch_config": {
                "project_dir": str(self.project_dir) if self.project_dir else None,
                "project_dir_display": project_dir_display,
                "project_dir_is_remote": self.remote_config is not None,
                "session_id": self.configured_resume_session_id,
                "session_mode": config.session_mode.value,
                "session_mode_locked": config.session_mode_locked,
                "remote": config.remote,
                "sandbox_backend": config.sandbox_backend,
                "sandbox_backend_default": config.sandbox_backend_default,
                "sandbox_backends": list(config.sandbox_backends),
                "can_run": config.can_run(),
                "can_resume": config.can_resume(),
            },
            "runtime": {
                "current_session_id": self.current_session_id,
                "configured_resume_session_id": self.configured_resume_session_id,
                "task": self.current_task,
                "model": self.selected_model or self._default_model_from_config(),
                "keep_pinned_messages": max(0, int(self.keep_pinned_messages)),
                "root_agent_name": self.root_agent_name,
                "selected_skill_ids": list(self.selected_skill_ids),
                "skills_state": self.skills_state,
                "available_skills": list(self.available_skills),
                "selected_mcp_server_ids": list(self.selected_mcp_server_ids),
                "mcp_state": self.mcp_state,
                "available_mcp_servers": list(self.available_mcp_servers),
                "session_status": self.current_session_status,
                "summary": self.current_summary,
                "status_message": self.status_message,
                "running": self.has_running_session(),
                "project_sync_action_in_progress": self.project_sync_action_in_progress,
            },
            "app_dir": str(self._resolved_app_dir()),
            "sessions_dir": str(self.sessions_root_dir()),
        }

    def set_locale(self, locale: str, *, persist_override: bool = True) -> None:
        desired = locale if locale in {"en", "zh"} else "en"
        if persist_override:
            self.locale_override = desired
        previous_ready = self.translator.text("ready")
        previous_required = self.translator.text("configuration_required")
        self.locale = desired
        self.translator = Translator(desired)
        if self.status_message in {previous_ready, previous_required}:
            fallback = (
                "ready"
                if self.launch_config().can_run() or self.launch_config().can_resume()
                else "configuration_required"
            )
            self.status_message = self.translator.text(fallback)

    def refresh_runtime_config(self) -> None:
        self.keep_pinned_messages = self._default_keep_pinned_messages_from_config()
        if self.locale_override in {"en", "zh"}:
            if not str(self.selected_model or "").strip():
                self.selected_model = self._default_model_from_config()
            return
        desired = self._resolve_configured_locale()
        if desired != self.locale:
            self.set_locale(desired, persist_override=False)
        if not str(self.selected_model or "").strip():
            self.selected_model = self._default_model_from_config()

    def set_launch_config(
        self,
        *,
        project_dir: str | None,
        session_id: str | None,
        session_mode: str | None = None,
        remote: dict[str, Any] | None = None,
        remote_password: str | None = None,
        sandbox_backend: str | None = None,
    ) -> dict[str, Any]:
        normalized_project = self._normalize_project_dir(project_dir) if project_dir else None
        normalized_session = self._normalize_optional_session_id(session_id)
        normalized_mode = normalize_workspace_mode(session_mode or self.session_mode.value)
        normalized_remote = (
            normalize_remote_session_config(remote) if isinstance(remote, dict) and remote else None
        )
        normalized_backend = self._normalize_sandbox_backend(sandbox_backend)
        if normalized_remote is not None and normalized_mode != WorkspaceMode.DIRECT:
            raise ValueError("Remote workspace is supported only in direct mode.")
        if normalized_project is not None and not normalized_project.is_dir():
            raise ValueError(self.translator.text("error_project_invalid"))
        supplied_remote_password = str(remote_password or "").strip()
        self.project_dir = normalized_project
        self.configured_resume_session_id = normalized_session
        self.remote_config = normalized_remote
        self.sandbox_backend = normalized_backend
        if supplied_remote_password:
            self.remote_password = supplied_remote_password
        elif normalized_remote is None or normalized_remote.auth_mode != "password":
            self.remote_password = ""
        if normalized_session:
            self._load_configured_session_context(normalized_session)
            self.status_message = self.translator.text("configuration_saved")
        else:
            self.session_mode = normalized_mode
            self.session_mode_locked = False
            if normalized_remote is not None:
                self.project_dir = None
            self._clear_session_runtime_context()
            if normalized_project is not None:
                self.status_message = self.translator.text("configuration_saved")
            elif normalized_remote is not None:
                self.status_message = self.translator.text("configuration_saved")
            elif not self.launch_config().can_run():
                self.status_message = self.translator.text("configuration_required")
        return self.snapshot()

    def _clear_session_runtime_context(self) -> None:
        self.current_session_id = None
        self.current_task = ""
        self.current_session_status = "idle"
        self.current_summary = ""
        self.selected_skill_ids = []
        self.skills_state = {}
        self.available_skills = []
        self.selected_mcp_server_ids = []
        self.mcp_state = {}
        self.available_mcp_servers = []

    def _load_configured_session_context(self, session_id: str) -> None:
        self._require_session_dir(session_id)
        session_dir = self.sessions_root_dir() / RuntimePaths.normalize_session_id(session_id)
        loaded_remote = load_remote_session_config(session_dir)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        loaded = orchestrator.load_session_context(session_id)
        self.remote_config = loaded_remote
        self.project_dir = None if loaded_remote is not None else loaded.project_dir.resolve()
        self.remote_password = ""
        self.configured_resume_session_id = loaded.id
        self.session_mode = normalize_workspace_mode(loaded.workspace_mode)
        self.session_mode_locked = True
        self.available_skills = []
        self.available_mcp_servers = []
        self._apply_session_runtime_state(loaded)

    def _apply_session_runtime_state(self, session: RunSession) -> None:
        self.current_session_id = session.id
        self.current_task = session.task
        self.current_session_status = session.status.value
        self.current_summary = session.final_summary or ""
        self.selected_skill_ids = list(session.enabled_skill_ids)
        self.skills_state = (
            dict(session.skills_state) if isinstance(session.skills_state, dict) else {}
        )
        self.selected_mcp_server_ids = list(session.enabled_mcp_server_ids)
        self.mcp_state = (
            dict(session.mcp_state) if isinstance(session.mcp_state, dict) else {}
        )

    @staticmethod
    def _normalize_mcp_server_ids(server_ids: list[str] | None) -> list[str] | None:
        if server_ids is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in server_ids:
            server_id = str(item).strip()
            if not server_id or server_id in seen:
                continue
            seen.add(server_id)
            normalized.append(server_id)
        return normalized

    def _project_dir_display(self) -> str | None:
        if self.remote_config is not None:
            remote_dir = str(self.remote_config.remote_dir or "").strip()
            return remote_dir or None
        return str(self.project_dir) if self.project_dir else None

    def _apply_session_project_location(self, session: RunSession) -> None:
        if self.remote_config is not None:
            self.project_dir = None
            return
        self.project_dir = session.project_dir.resolve()

    def has_running_session(self) -> bool:
        return bool(self.session_task and not self.session_task.done())

    async def start_run(
        self,
        task: str,
        model: str | None = None,
        root_agent_name: str | None = None,
        enabled_skill_ids: list[str] | None = None,
        enabled_mcp_server_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._state_lock:
            normalized_task = task.strip()
            if not normalized_task:
                raise RuntimeError(self.translator.text("error_task_required"))
            resolved_model = self._resolve_model_for_run(model)
            resolved_root_agent_name = str(root_agent_name or "").strip()
            normalized_skill_ids = (
                normalize_skill_ids(enabled_skill_ids)
                if enabled_skill_ids is not None
                else None
            )
            normalized_mcp_server_ids = self._normalize_mcp_server_ids(enabled_mcp_server_ids)
            self.selected_model = resolved_model
            self.root_agent_name = resolved_root_agent_name
            if normalized_skill_ids is not None:
                self.selected_skill_ids = list(normalized_skill_ids)
            if normalized_mcp_server_ids is not None:
                self.selected_mcp_server_ids = list(normalized_mcp_server_ids)
            resolved_session_id = self._normalize_optional_session_id(
                self.configured_resume_session_id
            )
            if self.has_running_session():
                submit_session_id = self._normalize_optional_session_id(
                    resolved_session_id or self.current_session_id
                )
                active_session_id = self._normalize_optional_session_id(self.current_session_id)
                if not submit_session_id:
                    raise RuntimeError(self.translator.text("already_running"))
                if active_session_id and submit_session_id != active_session_id:
                    raise RuntimeError(self.translator.text("already_running"))
                self._require_session_dir(submit_session_id)
                orchestrator = self.orchestrator
                if orchestrator is None:
                    raise RuntimeError(self.translator.text("already_running"))
                orchestrator.submit_run_in_active_session(
                    submit_session_id,
                    normalized_task,
                    model=resolved_model,
                    root_agent_name=resolved_root_agent_name or None,
                    enabled_mcp_server_ids=normalized_mcp_server_ids,
                    source="webui",
                )
                self.current_task = normalized_task
                self.current_session_id = submit_session_id
                self.configured_resume_session_id = submit_session_id
                self.current_session_status = "running"
                self.status_message = self.translator.text("started")
                return self.snapshot()
            if resolved_session_id:
                self._require_session_dir(resolved_session_id)
                self.current_task = normalized_task
                self.current_session_id = resolved_session_id
                self.current_session_status = "starting"
                self.current_summary = ""
                self.status_message = self.translator.text("started")
                self.orchestrator = self._create_orchestrator(self.project_dir or Path.cwd())
                self.orchestrator.subscribe(self._on_runtime_update)
                self.session_task = asyncio.create_task(
                    self._run_task_in_session(
                        resolved_session_id,
                        normalized_task,
                        resolved_model,
                        resolved_root_agent_name,
                        self.remote_password,
                        normalized_skill_ids,
                        normalized_mcp_server_ids,
                    )
                )
                return self.snapshot()
            has_remote = self.remote_config is not None
            if not has_remote and (self.project_dir is None or not self.project_dir.is_dir()):
                raise RuntimeError(self.translator.text("error_config_required"))
            self.current_task = normalized_task
            self.current_session_id = None
            self.current_session_status = "starting"
            self.current_summary = ""
            self.status_message = self.translator.text("started")
            self.orchestrator = self._create_orchestrator(self.project_dir or Path.cwd())
            self.orchestrator.subscribe(self._on_runtime_update)
            self.session_task = asyncio.create_task(
                self._run_task(
                    normalized_task,
                    resolved_model,
                    resolved_root_agent_name,
                    self.session_mode,
                    self.remote_config,
                    self.remote_password,
                    normalized_skill_ids,
                    normalized_mcp_server_ids,
                )
            )
        return self.snapshot()

    async def discover_skills(
        self,
        *,
        project_dir: str | None = None,
        remote: dict[str, Any] | RemoteSessionConfig | None = None,
        remote_password: str | None = None,
    ) -> dict[str, Any]:
        normalized_remote = (
            normalize_remote_session_config(remote)
            if isinstance(remote, (dict, RemoteSessionConfig)) and remote
            else None
        )
        normalized_project = (
            Path(project_dir).expanduser().resolve()
            if project_dir
            else self.project_dir
        )
        if normalized_remote is None and normalized_project is None:
            raise ValueError(self.translator.text("error_config_required"))
        orchestrator = self._read_orchestrator(normalized_project or Path.cwd())
        skills = await orchestrator.discover_skills(
            project_dir=None if normalized_remote is not None else normalized_project,
            remote_config=normalized_remote,
            remote_password=remote_password if normalized_remote is not None else None,
        )
        self.available_skills = skills
        return {
            "skills": skills,
            "snapshot": self.snapshot(),
        }

    async def discover_mcp_servers(self) -> dict[str, Any]:
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        servers = orchestrator.mcp_manager.available_servers()
        self.available_mcp_servers = list(servers)
        return {
            "mcp_servers": servers,
            "snapshot": self.snapshot(),
        }

    def interrupt(self) -> dict[str, Any]:
        orchestrator = self.orchestrator
        if not (orchestrator and self.has_running_session()):
            return self.snapshot()
        orchestrator.request_interrupt()
        self.current_session_status = "interrupting"
        self.status_message = self.translator.text("interrupted")
        return self.snapshot()

    async def shutdown(self) -> None:
        task = self.session_task
        if task is None or task.done():
            return
        if self.orchestrator is not None:
            self.orchestrator.request_interrupt()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.session_task = None

    def sessions_root_dir(self) -> Path:
        app_dir = self._resolved_app_dir()
        config = OpenCompanyConfig.load(app_dir)
        return RuntimePaths.create(app_dir, config).sessions_dir

    def list_session_directories(self) -> list[dict[str, Any]]:
        sessions_dir = self.sessions_root_dir()
        if not sessions_dir.exists():
            return []
        rows = self._read_orchestrator(self.project_dir or Path.cwd()).storage.list_sessions()
        rows_by_id = {str(row.get("id", "")): row for row in rows}
        directories = [item for item in sessions_dir.iterdir() if item.is_dir()]
        listed: list[dict[str, Any]] = []
        for directory in directories:
            row = rows_by_id.get(directory.name, {})
            continued_from_session_id: str | None = None
            raw_snapshot = row.get("config_snapshot_json")
            if isinstance(raw_snapshot, str) and raw_snapshot.strip():
                try:
                    parsed_snapshot = json.loads(raw_snapshot)
                except json.JSONDecodeError:
                    parsed_snapshot = None
                if isinstance(parsed_snapshot, dict):
                    normalized_parent = str(
                        parsed_snapshot.get("continued_from_session_id", "")
                    ).strip()
                    if normalized_parent:
                        continued_from_session_id = normalized_parent
            listed.append(
                {
                    "session_id": directory.name,
                    "path": str(directory),
                    "status": row.get("status"),
                    "task": row.get("task"),
                    "updated_at": row.get("updated_at"),
                    "project_dir": row.get("project_dir"),
                    "continued_from_session_id": continued_from_session_id,
                    "workspace_mode": normalize_workspace_mode(
                        row.get("workspace_mode", WorkspaceMode.STAGED.value)
                    ).value,
                }
            )
        listed.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("session_id") or ""),
            ),
            reverse=True,
        )
        return listed

    def browse_project_directories(self, path: str | None) -> dict[str, Any]:
        root = Path.home().resolve()
        requested = root if not path else Path(path).expanduser().resolve()
        if requested != root and root not in requested.parents:
            raise ValueError("Path is outside the allowed root.")
        if not requested.exists() or not requested.is_dir():
            raise ValueError("Path must be an existing directory.")
        parent = str(requested.parent) if requested != root else None
        entries = [
            {
                "name": entry.name,
                "path": str(entry.resolve()),
            }
            for entry in sorted(requested.iterdir(), key=lambda item: item.name.lower())
            if entry.is_dir()
        ]
        return {
            "root": str(root),
            "path": str(requested),
            "parent": parent,
            "entries": entries,
        }

    async def pick_project_directory(
        self,
        session_mode: str | None = None,
        *,
        sandbox_backend: str | None = None,
    ) -> dict[str, Any]:
        selected = self.prompt_for_directory(
            self.translator.text("path_picker_title"),
            self.project_dir or Path.home(),
        )
        if selected is None:
            raise RuntimeError(self.translator.text("picker_cancelled"))
        return self.set_launch_config(
            project_dir=str(selected),
            session_id=None,
            session_mode=session_mode,
            sandbox_backend=sandbox_backend,
        )

    async def pick_session_directory(self, *, sandbox_backend: str | None = None) -> dict[str, Any]:
        sessions_root = self.sessions_root_dir().resolve()
        selected = self.prompt_for_directory(
            self.translator.text("session_picker_title"),
            sessions_root,
        )
        if selected is None:
            raise RuntimeError(self.translator.text("picker_cancelled"))
        selected_path = selected.resolve()
        if selected_path.parent != sessions_root:
            raise ValueError(self.translator.text("error_session_folder_invalid"))
        await self.validate_remote_session_load(
            session_id=selected_path.name,
            sandbox_backend=sandbox_backend,
        )
        return self.set_launch_config(
            project_dir=None,
            session_id=selected_path.name,
            sandbox_backend=sandbox_backend,
        )

    def prompt_for_directory(self, title: str, initial_dir: Path) -> Path | None:
        selected = _open_native_directory_picker(title=title, initial_dir=initial_dir)
        if not selected:
            return None
        return Path(selected).expanduser().resolve()

    def load_session_events(self, session_id: str) -> list[dict[str, Any]]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.load_session_events(normalized)

    def list_session_events_page(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        before: str | None = None,
        activity_only: bool = False,
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.list_session_events_page(
            normalized,
            limit=limit,
            before=before,
            activity_only=activity_only,
        )

    def load_session_agents(self, session_id: str) -> list[dict[str, Any]]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        if hasattr(orchestrator, "load_session_agents"):
            return orchestrator.load_session_agents(normalized)
        return []

    def list_session_messages_page(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
        tail: int | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.list_session_messages(
            normalized,
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
            tail=tail,
            before=before,
        )

    def list_tool_runs_page(
        self,
        session_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.list_tool_runs_page(
            normalized,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    def get_tool_run(self, session_id: str, tool_run_id: str) -> dict[str, Any]:
        normalized_session_id = self._resolve_session_id(session_id)
        orchestrator = self._terminal_orchestrator()
        if hasattr(orchestrator, "get_tool_run_detail"):
            return orchestrator.get_tool_run_detail(normalized_session_id, tool_run_id)
        normalized_tool_run_id = str(tool_run_id or "").strip()
        if not normalized_tool_run_id:
            raise ValueError("tool_run_id is required")
        record = orchestrator.storage.load_tool_run(normalized_tool_run_id)
        if not isinstance(record, dict):
            raise ValueError(f"Tool run {normalized_tool_run_id} was not found.")
        if str(record.get("session_id", "")).strip() != normalized_session_id:
            raise ValueError(f"Tool run {normalized_tool_run_id} is outside the current session.")
        detail = dict(record)
        if str(detail.get("tool_name", "")).strip() == "shell" and hasattr(
            orchestrator,
            "_shell_outputs_for_tool_run",
        ):
            stdout, stderr = orchestrator._shell_outputs_for_tool_run(detail)
            detail["stdout"] = stdout
            detail["stderr"] = stderr
        detail.setdefault("timeline", [])
        return detail

    def tool_run_metrics(self, session_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.tool_run_metrics(normalized)

    async def submit_steer_run_with_activation(
        self,
        session_id: str,
        *,
        agent_id: str,
        content: str,
        source: str = "webui",
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            raise ValueError("agent_id is required.")
        async with self._state_lock:
            orchestrator = self._write_orchestrator(self.project_dir or Path.cwd())
            run = orchestrator.submit_steer_run(
                session_id=normalized,
                agent_id=normalized_agent_id,
                content=content,
                source=source,
            )
            target_role = self._agent_role_in_session(
                orchestrator=orchestrator,
                session_id=normalized,
                agent_id=normalized_agent_id,
            )
            run_root_agent = target_role in {"", "root"}
            session_row = orchestrator.storage.load_session(normalized)
            session_status = (
                str(session_row.get("status", "")).strip().lower()
                if isinstance(session_row, dict)
                else ""
            )
            if session_status == "running" or self.has_running_session():
                return run
            instruction = self._steer_resume_instruction(normalized_agent_id)
            selected_model = self._resolve_model_for_run(self.selected_model)
            self.selected_model = selected_model
            self.current_task = instruction
            self.current_session_id = normalized
            self.configured_resume_session_id = normalized
            self.current_session_status = "resuming"
            self.current_summary = ""
            self.status_message = self.translator.text("resume_started")
            self.orchestrator = orchestrator
            self.session_task = asyncio.create_task(
                self._continue_task(
                    normalized,
                    instruction,
                    selected_model,
                    reactivate_agent_id=normalized_agent_id,
                    run_root_agent=run_root_agent,
                    remote_password=self.remote_password,
                )
            )
            return run

    async def terminate_agent_with_subtree(
        self,
        session_id: str,
        *,
        agent_id: str,
        source: str = "webui",
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            raise ValueError("agent_id is required.")
        async with self._state_lock:
            orchestrator = self._write_orchestrator(self.project_dir or Path.cwd())
            result = await orchestrator.terminate_agent_subtree(
                session_id=normalized,
                agent_id=normalized_agent_id,
                source=source,
            )
            terminated_count = len(result.get("terminated_agent_ids", []))
            if terminated_count > 0:
                self.status_message = self.translator.text("agent_terminate_requested")
            else:
                self.status_message = self.translator.text("agent_terminate_noop")
            return result

    def list_steer_runs_page(
        self,
        session_id: str,
        *,
        status: str | list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.list_steer_runs_page(
            normalized,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    def steer_run_metrics(self, session_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.steer_run_metrics(normalized)

    def cancel_steer_run(self, session_id: str, steer_run_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._write_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.cancel_steer_run(
            session_id=normalized,
            steer_run_id=steer_run_id,
        )

    def project_sync_status(self, session_id: str) -> dict[str, Any] | None:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.project_sync_status(normalized)

    def project_sync_preview(self, session_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._read_orchestrator(self.project_dir or Path.cwd())
        return orchestrator.project_sync_preview(normalized, max_files=80, max_chars=200_000)

    async def apply_project_sync(self, session_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        if self.has_running_session():
            raise RuntimeError(self.translator.text("already_running"))
        self.project_sync_action_in_progress = True
        try:
            return await asyncio.to_thread(self._run_project_sync_action, "apply", normalized)
        finally:
            self.project_sync_action_in_progress = False

    async def undo_project_sync(self, session_id: str) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        if self.has_running_session():
            raise RuntimeError(self.translator.text("already_running"))
        self.project_sync_action_in_progress = True
        try:
            return await asyncio.to_thread(self._run_project_sync_action, "undo", normalized)
        finally:
            self.project_sync_action_in_progress = False

    def open_terminal(
        self,
        session_id: str | None = None,
        *,
        remote_password: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._resolve_session_id(session_id)
        orchestrator = self._terminal_orchestrator()
        if str(remote_password or "").strip():
            return orchestrator.open_session_terminal(
                normalized,
                remote_password=remote_password,
            )
        return orchestrator.open_session_terminal(normalized)

    async def validate_remote_workspace(
        self,
        *,
        remote: dict[str, Any],
        remote_password: str | None = None,
        session_mode: str | None = None,
        sandbox_backend: str | None = None,
    ) -> dict[str, Any]:
        normalized_mode = normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value)
        if normalized_mode != WorkspaceMode.DIRECT:
            raise ValueError("Remote workspace is supported only in direct mode.")
        normalized_remote = normalize_remote_session_config(remote)
        password = str(remote_password or "").strip()
        if normalized_remote.auth_mode == "password" and not password:
            raise ValueError("Remote password is required when auth_mode=password.")

        session_id = f"remote-validate-{uuid.uuid4().hex[:12]}"
        orchestrator = self._create_orchestrator(self.project_dir or Path.cwd())
        if sandbox_backend is not None:
            normalized_backend = self._normalize_sandbox_backend(
                sandbox_backend,
                fallback=self.sandbox_backend,
            )
            orchestrator.config.sandbox.backend = normalized_backend
            backend_cls = resolve_sandbox_backend_cls(orchestrator.config.sandbox)
            orchestrator.tool_executor.sandbox_backend_cls = backend_cls
            if hasattr(orchestrator.tool_executor, "_shell_backend_instance"):
                orchestrator.tool_executor._shell_backend_instance = None  # type: ignore[attr-defined]
        self.app_dir = orchestrator.app_dir
        try:
            orchestrator.tool_executor.set_session_remote_config(
                session_id,
                normalized_remote,
                password=password,
            )
            remote_context = orchestrator.tool_executor.session_remote_context(session_id)
            if remote_context is None:
                raise RuntimeError("Failed to initialize remote runtime context.")
            remote_root = Path(normalized_remote.remote_dir).expanduser()
            request = orchestrator.tool_executor.build_shell_request(
                workspace_root=remote_root,
                command=(
                    "set -euo pipefail; "
                    f"test -d {shlex.quote(normalized_remote.remote_dir)}; "
                    "uname -s"
                ),
                cwd=".",
                writable_paths=[remote_root],
                session_id=session_id,
                remote=remote_context,
            )
            setup_status_lines: list[str] = []

            async def on_event(channel: str, text: str) -> None:
                del channel
                if "[opencompany][remote-setup]" in str(text):
                    setup_status_lines.append(str(text))

            result = await orchestrator.tool_executor.shell_backend().run_command(
                request,
                on_event=on_event,
            )
            stdout_text = str(result.stdout or "").strip()
            status_text = "".join(setup_status_lines).strip()
            stderr_text = "\n".join(
                chunk for chunk in (status_text, str(result.stderr or "").strip()) if chunk
            ).strip()
            ok = result.exit_code == 0 and "linux" in stdout_text.lower()
            return {
                "ok": ok,
                "ssh_target": normalized_remote.ssh_target,
                "remote_dir": normalized_remote.remote_dir,
                "auth_mode": normalized_remote.auth_mode,
                "known_hosts_policy": normalized_remote.known_hosts_policy,
                "remote_os": normalized_remote.remote_os,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": int(result.exit_code),
            }
        finally:
            with suppress(Exception):
                orchestrator.tool_executor.cleanup_session_remote_runtime(session_id)

    async def validate_remote_session_load(
        self,
        *,
        session_id: str,
        sandbox_backend: str | None = None,
        remote_password: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            normalized_session = self._normalize_optional_session_id(session_id)
            if not normalized_session:
                return None
            self._require_session_dir(normalized_session)
            session_dir = (self.sessions_root_dir() / normalized_session).resolve()
            loaded_remote = load_remote_session_config(session_dir)
            if loaded_remote is None:
                return None
            normalized_backend = self._normalize_sandbox_backend(
                sandbox_backend,
                fallback=self.sandbox_backend,
            )
            if normalized_backend != "anthropic":
                return None
            password = str(remote_password or "").strip()
            if loaded_remote.auth_mode == "password" and not password and str(loaded_remote.password_ref or "").strip():
                with suppress(Exception):
                    password = str(load_remote_session_password(loaded_remote.password_ref) or "").strip()
            result = await self.validate_remote_workspace(
                remote={
                    "kind": loaded_remote.kind,
                    "ssh_target": loaded_remote.ssh_target,
                    "remote_dir": loaded_remote.remote_dir,
                    "auth_mode": loaded_remote.auth_mode,
                    "identity_file": loaded_remote.identity_file,
                    "known_hosts_policy": loaded_remote.known_hosts_policy,
                    "remote_os": loaded_remote.remote_os,
                },
                remote_password=password,
                session_mode=WorkspaceMode.DIRECT.value,
                sandbox_backend=normalized_backend,
            )
            if bool((result or {}).get("ok")):
                return result
            reason = str((result or {}).get("stderr") or (result or {}).get("stdout") or "").strip()
            summarized = self._summarize_remote_validate_output(reason)
            if summarized:
                raise ValueError(f"Remote validation failed: {summarized}")
            raise ValueError("Remote validation failed.")
        except ValueError:
            raise
        except Exception as exc:
            summarized = self._summarize_remote_validate_output(str(exc))
            if summarized:
                raise ValueError(f"Remote validation failed: {summarized}") from exc
            raise ValueError("Remote validation failed.") from exc

    @staticmethod
    def _summarize_remote_validate_output(
        value: str,
        *,
        max_lines: int = 12,
        max_chars: int = 1800,
    ) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > max_chars:
            text = f"{text[:max_chars].rstrip()} ..."
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join([*lines[:max_lines], "..."])
        return text

    def read_config(self) -> dict[str, Any]:
        path = self.config_file_path()
        if path.exists():
            text = path.read_text(encoding="utf-8")
            mtime_ns = path.stat().st_mtime_ns
        else:
            text = ""
            mtime_ns = None
        return {
            "path": str(path),
            "text": text,
            "mtime_ns": mtime_ns,
            "snapshot": self.snapshot(),
        }

    def read_config_meta(self) -> dict[str, Any]:
        path = self.config_file_path()
        return {
            "path": str(path),
            "exists": path.exists(),
            "mtime_ns": path.stat().st_mtime_ns if path.exists() else None,
            "snapshot": self.snapshot(),
        }

    def save_config(self, text: str) -> dict[str, Any]:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{self.translator.text('config_invalid_toml')}: {exc}") from exc
        path = self.config_file_path()
        path.write_text(text, encoding="utf-8")
        return {
            "path": str(path),
            "mtime_ns": path.stat().st_mtime_ns,
            "text": text,
            "snapshot": self.snapshot(),
        }

    def config_file_path(self) -> Path:
        return self._resolved_app_dir() / "opencompany.toml"

    def _normalize_project_dir(self, project_dir: str) -> Path:
        return Path(project_dir).expanduser().resolve()

    def _require_session_dir(self, session_id: str) -> None:
        normalized_session_id = RuntimePaths.normalize_session_id(session_id)
        session_dir = (self.sessions_root_dir() / normalized_session_id).resolve()
        sessions_root = self.sessions_root_dir().resolve()
        if (
            session_dir.parent != sessions_root
            or not session_dir.exists()
            or not session_dir.is_dir()
        ):
            raise ValueError(self.translator.text("error_session_required"))

    def _resolve_session_id(self, session_id: str | None) -> str:
        raw = session_id or self.current_session_id or self.configured_resume_session_id or ""
        normalized_text = str(raw).strip()
        if not normalized_text:
            raise ValueError(self.translator.text("error_session_required"))
        normalized = RuntimePaths.normalize_session_id(normalized_text)
        self._require_session_dir(normalized)
        return normalized

    def _normalize_optional_session_id(self, session_id: str | None) -> str | None:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        return RuntimePaths.normalize_session_id(normalized)

    def _read_orchestrator(self, project_dir: Path) -> Orchestrator:
        orchestrator = self._create_orchestrator(project_dir)
        self.app_dir = orchestrator.app_dir
        return orchestrator

    def _write_orchestrator(self, project_dir: Path) -> Orchestrator:
        orchestrator = self.orchestrator or self._create_orchestrator(project_dir)
        if not hasattr(orchestrator, "_subscriber"):
            orchestrator.subscribe(self._on_runtime_update)
        self.app_dir = orchestrator.app_dir
        return orchestrator

    def _run_project_sync_action(self, action: str, session_id: str) -> dict[str, Any]:
        orchestrator = self._create_orchestrator(self.project_dir or Path.cwd())
        self.app_dir = orchestrator.app_dir
        if action == "apply":
            return orchestrator.apply_project_sync(session_id)
        return orchestrator.undo_project_sync(session_id)

    def _terminal_orchestrator(self) -> Orchestrator:
        if self.orchestrator is not None:
            self._apply_sandbox_backend(self.orchestrator)
            return self.orchestrator
        orchestrator = self._create_orchestrator(self.project_dir or Path.cwd())
        self.orchestrator = orchestrator
        self.app_dir = orchestrator.app_dir
        return orchestrator

    def _on_runtime_update(self, record: dict[str, Any]) -> None:
        self._consume_runtime_update(record)
        self.event_hub.publish(record)

    def _consume_runtime_update(self, record: dict[str, Any]) -> None:
        details = record.get("payload", {})
        event_type = str(record.get("event_type", ""))
        if isinstance(details, dict):
            if "task" in details and details.get("task"):
                self.current_task = str(details.get("task"))
        session_id = record.get("session_id")
        if session_id:
            try:
                normalized_session_id = self._normalize_optional_session_id(str(session_id))
            except ValueError:
                normalized_session_id = None
            if normalized_session_id:
                self.current_session_id = normalized_session_id
                self.configured_resume_session_id = normalized_session_id
        if event_type in {"session_started", "session_resumed"}:
            self.current_session_status = "running"
        elif event_type == "session_context_imported":
            self.current_session_status = str(details.get("session_status", self.current_session_status))
            self.status_message = self.translator.text("configuration_saved")
        elif event_type == "session_skills_materialized":
            if isinstance(details, dict):
                self.selected_skill_ids = [
                    str(item).strip()
                    for item in details.get("enabled_skill_ids", [])
                    if str(item).strip()
                ]
                self.skills_state = {
                    **(self.skills_state if isinstance(self.skills_state, dict) else {}),
                    "bundle_root": str(details.get("skill_bundle_root", "") or ""),
                    "manifest_path": str(details.get("manifest_path", "") or ""),
                    "warnings": list(details.get("warnings", []))
                    if isinstance(details.get("warnings"), list)
                    else [],
                }
        elif event_type == "session_mcp_refreshed":
            if isinstance(details, dict):
                self.selected_mcp_server_ids = self._normalize_mcp_server_ids(
                    details.get("enabled_mcp_server_ids")
                    if isinstance(details.get("enabled_mcp_server_ids"), list)
                    else None
                ) or []
                mcp_state = details.get("mcp_state")
                if isinstance(mcp_state, dict):
                    self.mcp_state = dict(mcp_state)
        elif event_type == "session_interrupted":
            self.current_session_status = "interrupted"
            self.status_message = self.translator.text("session_interrupted")
        elif event_type == "session_failed":
            self.current_session_status = "failed"
            if isinstance(details, dict):
                self.current_summary = str(details.get("error", ""))
        elif event_type == "session_finalized":
            self.current_session_status = "completed"
            if isinstance(details, dict):
                self.current_summary = str(details.get("user_summary", ""))
            self.status_message = self.current_summary or self.translator.text("session_completed")
        elif event_type == "project_sync_staged":
            self.status_message = self.translator.text("sync_state_pending")
        elif event_type == "project_sync_applied":
            self.status_message = self.translator.text("sync_apply_done")
        elif event_type == "project_sync_reverted":
            self.status_message = self.translator.text("sync_undo_done")

    async def _run_task(
        self,
        task: str,
        model: str,
        root_agent_name: str | None = None,
        session_mode: WorkspaceMode | str | None = None,
        remote: RemoteSessionConfig | None = None,
        remote_password: str | None = None,
        enabled_skill_ids: list[str] | None = None,
        enabled_mcp_server_ids: list[str] | None = None,
    ) -> None:
        orchestrator = self.orchestrator
        if orchestrator is None:
            return
        try:
            run_kwargs: dict[str, Any] = {
                "model": model,
                "root_agent_name": root_agent_name or None,
            }
            if normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value) != WorkspaceMode.DIRECT:
                run_kwargs["workspace_mode"] = session_mode
            if remote is not None:
                run_kwargs["remote_config"] = remote
                run_kwargs["remote_password"] = remote_password
            if enabled_skill_ids is not None:
                run_kwargs["enabled_skill_ids"] = enabled_skill_ids
            if enabled_mcp_server_ids is not None:
                run_kwargs["enabled_mcp_server_ids"] = enabled_mcp_server_ids
            session = await orchestrator.run_task(task, **run_kwargs)
            self._apply_session_project_location(session)
            self.configured_resume_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self._apply_session_runtime_state(session)
            self.status_message = self.current_summary or self.translator.text("session_completed")
        except asyncio.CancelledError:
            self.current_session_status = "interrupted"
            self.status_message = self.translator.text("session_interrupted")
            raise
        except Exception as exc:
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self.status_message = f"{self.translator.text('session_failed')}: {exc}"
        finally:
            self.session_task = None

    async def _run_task_in_session(
        self,
        session_id: str,
        task: str,
        model: str,
        root_agent_name: str | None = None,
        remote_password: str | None = None,
        enabled_skill_ids: list[str] | None = None,
        enabled_mcp_server_ids: list[str] | None = None,
    ) -> None:
        orchestrator = self.orchestrator
        if orchestrator is None:
            return
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "root_agent_name": root_agent_name or None,
            }
            if str(remote_password or "").strip():
                kwargs["remote_password"] = remote_password
            if enabled_skill_ids is not None:
                kwargs["enabled_skill_ids"] = enabled_skill_ids
            if enabled_mcp_server_ids is not None:
                kwargs["enabled_mcp_server_ids"] = enabled_mcp_server_ids
            session = await orchestrator.run_task_in_session(
                session_id,
                task,
                **kwargs,
            )
            self._apply_session_project_location(session)
            self.configured_resume_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self._apply_session_runtime_state(session)
            self.status_message = self.current_summary or self.translator.text("session_completed")
        except asyncio.CancelledError:
            self.current_session_status = "interrupted"
            self.status_message = self.translator.text("session_interrupted")
            raise
        except Exception as exc:
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self.status_message = f"{self.translator.text('session_failed')}: {exc}"
        finally:
            self.session_task = None

    async def _continue_task(
        self,
        session_id: str,
        instruction: str,
        model: str,
        reactivate_agent_id: str | None = None,
        run_root_agent: bool = True,
        remote_password: str | None = None,
        enabled_skill_ids: list[str] | None = None,
        enabled_mcp_server_ids: list[str] | None = None,
    ) -> None:
        orchestrator = self.orchestrator
        if orchestrator is None:
            return
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "reactivate_agent_id": reactivate_agent_id,
                "run_root_agent": run_root_agent,
            }
            if str(remote_password or "").strip():
                kwargs["remote_password"] = remote_password
            if enabled_skill_ids is not None:
                kwargs["enabled_skill_ids"] = enabled_skill_ids
            if enabled_mcp_server_ids is not None:
                kwargs["enabled_mcp_server_ids"] = enabled_mcp_server_ids
            session = await orchestrator.resume(
                session_id,
                instruction,
                **kwargs,
            )
            self._apply_session_project_location(session)
            self.configured_resume_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self._apply_session_runtime_state(session)
            self.status_message = self.current_summary or self.translator.text("session_resumed_done")
        except asyncio.CancelledError:
            self.current_session_status = "interrupted"
            self.status_message = self.translator.text("session_interrupted")
            raise
        except Exception as exc:
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self.status_message = f"{self.translator.text('session_failed')}: {exc}"
        finally:
            self.session_task = None

    def _create_orchestrator(self, project_dir: Path) -> Orchestrator:
        if self.debug_enabled:
            orchestrator = Orchestrator(project_dir, locale=self.locale, app_dir=self.app_dir, debug=True)
        else:
            orchestrator = Orchestrator(project_dir, locale=self.locale, app_dir=self.app_dir)
        self._apply_sandbox_backend(orchestrator)
        return orchestrator

    def _resolved_app_dir(self) -> Path:
        if self.app_dir is not None:
            return self.app_dir.resolve()
        if self.orchestrator is not None:
            orchestrator_app_dir = getattr(self.orchestrator, "app_dir", None)
            if orchestrator_app_dir is not None:
                return Path(orchestrator_app_dir).resolve()
        return default_app_dir()

    def _resolve_configured_locale(self, requested_locale: str | None = None) -> str:
        if requested_locale in {"en", "zh"}:
            return requested_locale
        try:
            config = OpenCompanyConfig.load(self._resolved_app_dir())
            return config.resolve_locale(requested_locale)
        except Exception:
            return "en"

    def _default_model_from_config(self) -> str:
        fallback = OpenCompanyConfig().llm.openrouter.model
        try:
            configured = str(OpenCompanyConfig.load(self._resolved_app_dir()).llm.openrouter.model).strip()
            return configured or fallback
        except Exception:
            return fallback

    def _default_keep_pinned_messages_from_config(self) -> int:
        fallback = max(0, int(OpenCompanyConfig().runtime.context.keep_pinned_messages))
        try:
            configured = int(
                OpenCompanyConfig.load(self._resolved_app_dir()).runtime.context.keep_pinned_messages
            )
        except Exception:
            return fallback
        return max(0, configured)

    @staticmethod
    def _available_sandbox_backends() -> tuple[str, ...]:
        backends = tuple(str(name).strip().lower() for name in available_sandbox_backends() if str(name).strip())
        if backends:
            return backends
        return ("anthropic", "none")

    def _default_sandbox_backend_from_config(self) -> str:
        try:
            configured = str(OpenCompanyConfig.load(self._resolved_app_dir()).sandbox.backend).strip()
        except Exception:
            configured = ""
        return self._normalize_sandbox_backend(configured, fallback="anthropic")

    def _normalize_sandbox_backend(self, backend: str | None, *, fallback: str | None = None) -> str:
        candidate = str(backend or "").strip().lower()
        if candidate in self._sandbox_backends:
            return candidate
        fallback_candidate = str(fallback or "").strip().lower()
        if fallback_candidate in self._sandbox_backends:
            return fallback_candidate
        return self._sandbox_backends[0]

    def _apply_sandbox_backend(self, orchestrator: Orchestrator) -> None:
        backend_name = self._normalize_sandbox_backend(
            self.sandbox_backend,
            fallback=self.sandbox_backend_default,
        )
        orchestrator.config.sandbox.backend = backend_name
        backend_cls = resolve_sandbox_backend_cls(orchestrator.config.sandbox)
        orchestrator.tool_executor.sandbox_backend_cls = backend_cls
        if hasattr(orchestrator.tool_executor, "_shell_backend_instance"):
            orchestrator.tool_executor._shell_backend_instance = None  # type: ignore[attr-defined]
        self.sandbox_backend = backend_name

    def _resolve_model_for_run(self, model: str | None) -> str:
        normalized = str(model or "").strip()
        if normalized:
            return normalized
        return self._default_model_from_config()

    def _remote_config_payload(self) -> dict[str, Any] | None:
        config = self.remote_config
        if config is None:
            return None
        return {
            "kind": config.kind,
            "ssh_target": config.ssh_target,
            "remote_dir": config.remote_dir,
            "auth_mode": config.auth_mode,
            "identity_file": config.identity_file,
            "known_hosts_policy": config.known_hosts_policy,
            "remote_os": config.remote_os,
            "password_saved": bool(str(config.password_ref or "").strip()),
        }

    @staticmethod
    def _steer_resume_instruction(agent_id: str) -> str:
        return (
            "A steer message was submitted while this session was inactive. "
            f"Reactivate agent {agent_id} and continue execution so pending steer instructions are consumed."
        )

    @staticmethod
    def _agent_role_in_session(
        *,
        orchestrator: Orchestrator,
        session_id: str,
        agent_id: str,
    ) -> str:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return ""
        load_agents = getattr(orchestrator, "load_session_agents", None)
        if callable(load_agents):
            rows = load_agents(session_id)
        else:
            rows = orchestrator.storage.load_agents(session_id)
        for row in rows:
            if str(row.get("id", "")).strip() != normalized_agent_id:
                continue
            return str(row.get("role", "")).strip().lower()
        return ""


def _open_native_directory_picker(*, title: str, initial_dir: Path) -> str | None:
    if sys.platform == "darwin":
        return _open_macos_directory_picker(title=title, initial_dir=initial_dir)

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on host environment.
        raise RuntimeError("Native directory picker is unavailable in this environment.") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    root.update()
    try:
        selected = filedialog.askdirectory(
            title=title,
            initialdir=str(initial_dir),
            mustexist=True,
        )
    finally:
        root.destroy()
    normalized = str(selected or "").strip()
    return normalized or None


def _open_macos_directory_picker(*, title: str, initial_dir: Path) -> str | None:
    safe_title = _escape_applescript_string(title)
    initial = initial_dir if initial_dir.exists() else Path.home()
    safe_initial = _escape_applescript_string(str(initial))

    command = [
        "osascript",
        "-e",
        f'set defaultFolder to POSIX file "{safe_initial}"',
        "-e",
        'tell application "System Events" to activate',
        "-e",
        f'set chosenFolder to choose folder with prompt "{safe_title}" default location defaultFolder',
        "-e",
        "POSIX path of chosenFolder",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # pragma: no cover - depends on host environment.
        raise RuntimeError("Native directory picker is unavailable in this environment.") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "cancel" in stderr:
            return None
        raise RuntimeError((completed.stderr or "Failed to open directory picker.").strip())

    selected = (completed.stdout or "").strip()
    return selected or None


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
