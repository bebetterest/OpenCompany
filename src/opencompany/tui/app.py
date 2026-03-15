from __future__ import annotations

import asyncio
import json
import re
import shlex
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from textwrap import shorten
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    DirectoryTree,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from opencompany.config import OpenCompanyConfig
from opencompany.i18n import Translator
from opencompany.logging import DiagnosticLogger, diagnostics_path_for_app
from opencompany.models import RemoteSessionConfig, WorkspaceMode, normalize_workspace_mode
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.paths import RuntimePaths
from opencompany.protocol import extract_json_object
from opencompany.remote import (
    load_remote_session_config,
    load_remote_session_password,
    normalize_remote_session_config,
)
from opencompany.sandbox.registry import available_sandbox_backends, resolve_sandbox_backend_cls
from opencompany.tools.runtime import tool_run_duration_ms

LIVE_AGENT_META_SKIP_EVENTS = {
    "llm_reasoning",
    "llm_token",
    "tool_call_started",
    "tool_call",
    "tool_run_submitted",
    "tool_run_updated",
    "steer_run_submitted",
    "steer_run_updated",
}
ACTIVITY_SKIP_EVENTS = {
    "llm_reasoning",
    "llm_token",
    "shell_stream",
    "agent_response",
    "tool_call_started",
    "tool_call",
    "tool_run_submitted",
    "tool_run_updated",
    "steer_run_submitted",
    "steer_run_updated",
}


@dataclass(slots=True)
class SessionLaunchConfig:
    project_dir: Path | None = None
    session_id: str | None = None
    session_mode: WorkspaceMode = WorkspaceMode.DIRECT
    session_mode_locked: bool = False
    sandbox_backend: str = "anthropic"
    remote: dict[str, Any] | None = None
    remote_password: str = ""

    @classmethod
    def create(
        cls,
        project_dir: Path | None,
        session_id: str | None,
        *,
        session_mode: WorkspaceMode | str | None = None,
        session_mode_locked: bool = False,
        sandbox_backend: str | None = None,
        remote: dict[str, Any] | None = None,
        remote_password: str | None = None,
    ) -> SessionLaunchConfig:
        normalized_project = project_dir.resolve() if project_dir else None
        normalized_session = (session_id or "").strip() or None
        return cls(
            project_dir=normalized_project,
            session_id=normalized_session,
            session_mode=normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value),
            session_mode_locked=bool(session_mode_locked),
            sandbox_backend=str(sandbox_backend or "anthropic").strip().lower() or "anthropic",
            remote=remote if isinstance(remote, dict) else None,
            remote_password=str(remote_password or ""),
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


@dataclass(slots=True)
class AgentRuntimeView:
    id: str
    name: str
    instruction: str = ""
    role: str = "worker"
    parent_agent_id: str | None = None
    status: str = "pending"
    step_count: int = 0
    last_event: str = "idle"
    last_detail: str = ""
    last_phase: str = "runtime"
    last_timestamp: str = ""
    raw_llm_buffer: str = ""
    raw_reasoning_buffer: str = ""
    stream_entries: list[tuple[str, str]] = field(default_factory=list)
    step_entries: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    step_order: list[int] = field(default_factory=list)
    is_generating: bool = False
    summary: str = ""
    output_tokens_total: int = 0
    current_context_tokens: int = 0
    context_limit_tokens: int = 0
    usage_ratio: float = 0.0
    last_usage_input_tokens: int | None = None
    last_usage_output_tokens: int | None = None
    last_usage_cache_read_tokens: int | None = None
    last_usage_cache_write_tokens: int | None = None
    last_usage_total_tokens: int | None = None
    compression_count: int = 0
    keep_pinned_messages: int = 1
    summary_version: int = 0
    context_latest_summary: str = ""
    summarized_until_message_index: int | None = None
    last_compacted_step_range: str = ""
    compacted_step_ranges: list[tuple[int, int]] = field(default_factory=list)
    model: str = ""
    next_message_step: int = 1
    last_message_index: int = -1


class RuntimeUpdate(Message):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__()


class AgentCollapsible(Collapsible):
    def __init__(
        self,
        *children: Widget,
        agent_id: str,
        panel_kind: str,
        title: str,
        collapsed: bool,
        id: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.panel_kind = panel_kind
        classes = "agent-collapsible"
        if panel_kind == "live":
            classes = f"{classes} live-agent-card"
        super().__init__(
            *children,
            title=title,
            collapsed=collapsed,
            classes=classes,
            id=id,
        )


class AgentSectionCollapsible(Collapsible):
    def __init__(
        self,
        *children: Widget,
        agent_id: str,
        section_kind: str,
        title: str,
        collapsed: bool,
        id: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.section_kind = section_kind
        super().__init__(
            *children,
            title=title,
            collapsed=collapsed,
            classes="agent-section-collapsible",
            id=id,
        )


class LiveStepCollapsible(Collapsible):
    def __init__(
        self,
        *children: Widget,
        agent_id: str,
        step_number: int,
        title: str,
        collapsed: bool,
        id: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.step_number = step_number
        super().__init__(
            *children,
            title=title,
            collapsed=collapsed,
            classes="live-step-collapsible",
            id=id,
        )


class AgentJumpButton(Button):
    def __init__(
        self,
        label: str,
        *,
        target_agent_id: str,
        id: str | None = None,
    ) -> None:
        self.target_agent_id = target_agent_id
        super().__init__(label, id=id, classes="agent-jump-button")


class AgentSteerButton(Button):
    def __init__(
        self,
        label: str,
        *,
        target_agent_id: str,
        id: str | None = None,
    ) -> None:
        self.target_agent_id = target_agent_id
        super().__init__(label, id=id, classes="agent-jump-button")


class AgentTerminateButton(Button):
    def __init__(
        self,
        label: str,
        *,
        target_agent_id: str,
        id: str | None = None,
    ) -> None:
        self.target_agent_id = target_agent_id
        super().__init__(label, id=id, classes="agent-jump-button agent-terminate-button")


class AgentCopyButton(Button):
    def __init__(
        self,
        label: str,
        *,
        copy_value: str,
        copy_kind: str,
        id: str | None = None,
    ) -> None:
        self.copy_value = copy_value
        self.copy_kind = copy_kind
        super().__init__(label, id=id, classes="agent-jump-button")


class SteerRunCancelButton(Button):
    def __init__(
        self,
        label: str,
        *,
        steer_run_id: str,
        id: str | None = None,
    ) -> None:
        self.steer_run_id = steer_run_id
        super().__init__(label, id=id, classes="agent-jump-button")


class SessionPickerEntryButton(Button):
    def __init__(
        self,
        label: str,
        *,
        session_path: Path,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self.session_path = session_path
        super().__init__(label, id=id, classes=classes)


MAIN_CONTROL_BUTTON_IDS = {
    "locale_en_button",
    "locale_zh_button",
    "run_button",
    "terminal_button",
    "apply_button",
    "undo_button",
    "reconfigure_button",
    "interrupt_button",
    "config_save_button",
    "config_reload_button",
    "tool_runs_refresh_button",
    "tool_runs_filter_button",
    "tool_runs_group_button",
    "tool_runs_prev_button",
    "tool_runs_next_button",
    "tool_runs_detail_button",
    "steer_runs_refresh_button",
    "steer_runs_filter_button",
    "steer_runs_group_button",
}
TASK_INPUT_MIN_HEIGHT = 3
TASK_INPUT_MAX_HEIGHT = 9


class PathPickerScreen(ModalScreen[Path | None]):
    CSS = """
    #path_picker_dialog {
        width: 90%;
        height: 88%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #path_picker_value {
        margin: 1 0;
    }

    #path_picker_tree {
        height: 1fr;
        border: round $accent;
    }

    #path_picker_session_list {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }

    #path_picker_session_empty {
        color: $text-muted;
        margin: 1 0;
    }

    .path-picker-session-entry {
        width: 100%;
        margin: 0;
    }

    #path_picker_buttons {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        translator: Translator,
        initial_path: Path | None,
        root_path: Path | None = None,
        title_text: str | None = None,
        selected_label: str | None = None,
        confirm_label: str | None = None,
        session_picker: bool = False,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.title_text = str(title_text or self.translator.text("path_picker_title"))
        self.selected_label = str(selected_label or self.translator.text("selected_project_dir"))
        self.confirm_label = str(confirm_label or self.translator.text("select_path"))
        self.root_path = (
            root_path.resolve() if root_path is not None else self._determine_root_path(initial_path)
        )
        self.session_picker = bool(session_picker)
        if initial_path and initial_path.is_dir():
            self.selected_path = initial_path.resolve()
        else:
            self.selected_path = self.root_path
        self._session_entries: list[tuple[Path, str, float]] = []
        if self.session_picker:
            self._session_entries = self._build_session_entries()

    def compose(self) -> ComposeResult:
        with Vertical(id="path_picker_dialog"):
            yield Static(self.title_text)
            yield Static("", id="path_picker_value")
            yield DirectoryTree(str(self.root_path), id="path_picker_tree")
            with VerticalScroll(id="path_picker_session_list"):
                if self._session_entries:
                    for index, (path, label, _) in enumerate(self._session_entries):
                        yield SessionPickerEntryButton(
                            label,
                            session_path=path,
                            id=f"path_picker_session_entry_{index}",
                            classes="path-picker-session-entry",
                        )
                else:
                    yield Static(
                        self.translator.text("session_picker_empty"),
                        id="path_picker_session_empty",
                    )
            with Horizontal(id="path_picker_buttons"):
                yield Button(self.confirm_label, id="path_picker_confirm", variant="primary")
                yield Button(self.translator.text("cancel"), id="path_picker_cancel")

    def on_mount(self) -> None:
        tree = self.query_one("#path_picker_tree", DirectoryTree)
        session_list = self.query_one("#path_picker_session_list", VerticalScroll)
        if self.session_picker:
            tree.display = False
            session_list.display = True
            self._refresh_session_entry_variants()
            first_entry = self._query_optional("#path_picker_session_entry_0", SessionPickerEntryButton)
            if first_entry is not None:
                first_entry.focus()
        else:
            tree.display = True
            session_list.display = False
            tree.root.expand()
            tree.focus()
        self._render_selected_path()

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if self.session_picker:
            return
        self.selected_path = Path(str(event.path)).resolve()
        self._render_selected_path()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, SessionPickerEntryButton):
            self.selected_path = event.button.session_path.resolve()
            self._render_selected_path()
            self._refresh_session_entry_variants()
            return
        if event.button.id == "path_picker_confirm":
            self.dismiss(self.selected_path)
        elif event.button.id == "path_picker_cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _determine_root_path(self, initial_path: Path | None) -> Path:
        home = Path.home().resolve()
        if initial_path:
            resolved = initial_path.resolve()
            if resolved == home or home in resolved.parents:
                return home
            if resolved.anchor:
                return Path(resolved.anchor)
        return home

    def _render_selected_path(self) -> None:
        self.query_one("#path_picker_value", Static).update(
            f"{self.selected_label}: {self.selected_path}"
        )

    def _build_session_entries(self) -> list[tuple[Path, str, float]]:
        if not self.root_path.exists():
            return []
        entries: list[tuple[Path, str, float]] = []
        for directory in self.root_path.iterdir():
            if not directory.is_dir():
                continue
            timestamp = self._session_timestamp(directory)
            time_label = self._format_session_time(timestamp)
            label = (
                f"{directory.name}  "
                f"[{self.translator.text('session_picker_updated_at')}: {time_label}]"
            )
            entries.append((directory.resolve(), label, timestamp))
        entries.sort(key=lambda item: (-item[2], item[0].name))
        return entries

    @staticmethod
    def _session_timestamp(directory: Path) -> float:
        candidates = [directory / "events.jsonl", directory]
        for candidate in candidates:
            with suppress(Exception):
                if candidate.exists():
                    return float(candidate.stat().st_mtime)
        return 0.0

    def _format_session_time(self, timestamp: float) -> str:
        if timestamp <= 0:
            return self.translator.text("none_value")
        with suppress(Exception):
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return self.translator.text("none_value")

    def _refresh_session_entry_variants(self) -> None:
        for button in self.query(SessionPickerEntryButton):
            button.variant = (
                "primary"
                if button.session_path.resolve() == self.selected_path.resolve()
                else "default"
            )

    def _query_optional(self, selector: str, widget_type: type[Widget]) -> Widget | None:
        try:
            widget = self.query_one(selector)
        except NoMatches:
            return None
        return widget if isinstance(widget, widget_type) else None


class SessionConfigScreen(ModalScreen[SessionLaunchConfig | None]):
    CSS = """
    #launch_config_dialog {
        width: 88%;
        height: 90%;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #launch_config_scroll {
        height: 1fr;
        margin-top: 1;
    }

    #launch_config_help {
        margin-top: 0;
    }

    #launch_mode_buttons {
        height: auto;
        margin-top: 0;
    }

    #launch_mode_status {
        margin-top: 1;
    }

    #launch_project_section {
        margin-top: 1;
        height: auto;
    }

    #launch_workspace_mode_buttons {
        height: auto;
        margin-top: 1;
    }

    #launch_workspace_mode_status {
        margin: 1 0;
    }

    #launch_sandbox_backend_buttons {
        height: auto;
        margin-top: 1;
    }

    #launch_sandbox_backend_status {
        margin: 1 0;
    }

    #launch_workspace_source_buttons {
        height: auto;
    }

    #launch_project_local_section,
    #launch_project_remote_section {
        margin-top: 1;
        height: auto;
    }

    #launch_remote_target_label,
    #launch_remote_dir_label,
    #launch_remote_key_label,
    #launch_remote_password_label {
        margin-top: 1;
    }

    #launch_remote_key_row,
    #launch_remote_password_row {
        margin-top: 1;
        height: auto;
    }

    #launch_project_value {
        margin: 1 0;
    }

    #launch_project_buttons {
        height: auto;
    }

    #launch_remote_auth_buttons,
    #launch_remote_known_hosts_buttons {
        height: auto;
        margin-top: 1;
    }

    #launch_remote_validate_status {
        margin-top: 1;
        min-height: 1;
    }

    #launch_resume_section {
        margin-top: 1;
        height: auto;
    }

    #launch_session_value {
        margin: 1 0;
    }

    #launch_resume_validate_status {
        margin-top: 1;
        min-height: 1;
    }

    #launch_session_buttons {
        height: auto;
    }

    #launch_config_error {
        color: $error;
        min-height: 1;
        margin-top: 1;
    }

    #launch_config_buttons {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        translator: Translator,
        initial_config: SessionLaunchConfig,
        sessions_dir: Path,
        sandbox_backends: tuple[str, ...],
        sandbox_backend_default: str,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.project_dir = initial_config.project_dir
        self.sessions_dir = sessions_dir.resolve()
        self.session_dir = self._resolve_initial_session_dir(initial_config.session_id)
        self.session_mode = normalize_workspace_mode(initial_config.session_mode)
        self.session_mode_locked = bool(initial_config.session_mode_locked)
        self.sandbox_backends = tuple(
            str(name).strip().lower() for name in sandbox_backends if str(name).strip()
        ) or ("anthropic", "none")
        self.sandbox_backend_default = self._normalize_sandbox_backend(
            sandbox_backend_default,
            fallback="anthropic",
        )
        self.sandbox_backend = self._normalize_sandbox_backend(
            initial_config.sandbox_backend,
            fallback=self.sandbox_backend_default,
        )
        initial_remote = (
            normalize_remote_session_config(initial_config.remote)
            if isinstance(initial_config.remote, dict) and initial_config.remote
            else None
        )
        self.workspace_source = "remote" if initial_remote is not None else "local"
        self.remote_target = initial_remote.ssh_target if initial_remote is not None else ""
        self.remote_dir = initial_remote.remote_dir if initial_remote is not None else ""
        self.remote_auth_mode = initial_remote.auth_mode if initial_remote is not None else "key"
        self.remote_key_path = initial_remote.identity_file if initial_remote is not None else ""
        self.remote_known_hosts_policy = (
            initial_remote.known_hosts_policy if initial_remote is not None else "accept_new"
        )
        self.remote_password = str(initial_config.remote_password or "")
        self.remote_validate_busy = False
        self.remote_validate_ok: bool | None = None
        if initial_remote is not None:
            self.remote_validate_status = (
                f"{self.translator.text('configuration_ready')}: "
                f"{initial_remote.ssh_target}:{initial_remote.remote_dir}"
            )
        else:
            self.remote_validate_status = ""
        self._remote_validate_status_rendered = ""
        self._remote_validate_status_color_rendered = "default"
        self._resume_validate_status_rendered = ""
        self._resume_validate_status_color_rendered = "default"
        self.mode = "resume" if initial_config.session_id else "project"

    def compose(self) -> ComposeResult:
        with Vertical(id="launch_config_dialog"):
            yield Static(self.translator.text("launch_config_title"))
            yield Static(self.translator.text("launch_config_help"), id="launch_config_help")
            with VerticalScroll(id="launch_config_scroll"):
                with Horizontal(id="launch_mode_buttons"):
                    yield Button("", id="launch_mode_project_button", variant="primary")
                    yield Button("", id="launch_mode_resume_button")
                yield Static("", id="launch_mode_status")
                with Horizontal(id="launch_sandbox_backend_buttons"):
                    yield Button("", id="launch_sandbox_backend_anthropic_button", variant="primary")
                    yield Button("", id="launch_sandbox_backend_none_button")
                yield Static("", id="launch_sandbox_backend_status")
                with Vertical(id="launch_project_section"):
                    with Horizontal(id="launch_workspace_mode_buttons"):
                        yield Button("", id="launch_workspace_mode_direct_button", variant="primary")
                        yield Button("", id="launch_workspace_mode_staged_button")
                    yield Static("", id="launch_workspace_mode_status")
                    with Horizontal(id="launch_workspace_source_buttons"):
                        yield Button("", id="launch_workspace_source_local_button", variant="primary")
                        yield Button("", id="launch_workspace_source_remote_button")
                    with Vertical(id="launch_project_local_section"):
                        yield Static("", id="launch_project_value")
                        with Horizontal(id="launch_project_buttons"):
                            yield Button(
                                self.translator.text("choose_project_dir"),
                                id="choose_project_dir",
                                variant="primary",
                            )
                    with Vertical(id="launch_project_remote_section"):
                        yield Static("", id="launch_remote_help")
                        yield Static("", id="launch_remote_target_label")
                        yield Input("", id="launch_remote_target_input")
                        yield Static("", id="launch_remote_dir_label")
                        yield Input("", id="launch_remote_dir_input")
                        with Horizontal(id="launch_remote_auth_buttons"):
                            yield Button("", id="launch_remote_auth_key_button", variant="primary")
                            yield Button("", id="launch_remote_auth_password_button")
                        with Vertical(id="launch_remote_key_row"):
                            yield Static("", id="launch_remote_key_label")
                            yield Input("", id="launch_remote_key_input")
                        with Vertical(id="launch_remote_password_row"):
                            yield Static("", id="launch_remote_password_label")
                            yield Input("", password=True, id="launch_remote_password_input")
                        with Horizontal(id="launch_remote_known_hosts_buttons"):
                            yield Button(
                                "", id="launch_remote_known_hosts_accept_new_button", variant="primary"
                            )
                            yield Button("", id="launch_remote_known_hosts_strict_button")
                        yield Static("", id="launch_remote_validate_status")
                with Vertical(id="launch_resume_section"):
                    yield Static("", id="launch_session_value")
                    with Horizontal(id="launch_session_buttons"):
                        yield Button(
                            self.translator.text("choose_session_dir"),
                            id="choose_session_dir",
                            variant="primary",
                        )
                    yield Static("", id="launch_resume_validate_status")
                yield Static("", id="launch_config_error")
            with Horizontal(id="launch_config_buttons"):
                yield Button(self.translator.text("continue"), id="save_launch_config", variant="primary")
                yield Button(self.translator.text("cancel"), id="cancel_launch_config")

    def on_mount(self) -> None:
        self._render_mode()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch_mode_project_button":
            self.mode = "project"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_mode_resume_button":
            self.mode = "resume"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_sandbox_backend_anthropic_button":
            self.sandbox_backend = self._normalize_sandbox_backend("anthropic")
            self._set_error("")
            self._render_mode()
        elif event.button.id == "launch_sandbox_backend_none_button":
            self.sandbox_backend = self._normalize_sandbox_backend("none")
            self._set_error("")
            self._render_mode()
        elif event.button.id == "launch_workspace_mode_direct_button":
            if self._workspace_mode_locked():
                return
            self.session_mode = WorkspaceMode.DIRECT
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_workspace_mode_staged_button":
            if self._workspace_mode_locked():
                return
            self.session_mode = WorkspaceMode.STAGED
            if self.workspace_source == "remote":
                self.workspace_source = "local"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_workspace_source_local_button":
            self.workspace_source = "local"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_workspace_source_remote_button":
            if self.session_mode != WorkspaceMode.DIRECT:
                self._set_error(self.translator.text("remote_requires_direct_mode"))
                return
            self.workspace_source = "remote"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_remote_auth_key_button":
            if self.remote_auth_mode == "key":
                return
            self.remote_auth_mode = "key"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_remote_auth_password_button":
            if self.remote_auth_mode == "password":
                return
            self.remote_auth_mode = "password"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_remote_known_hosts_accept_new_button":
            self.remote_known_hosts_policy = "accept_new"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "launch_remote_known_hosts_strict_button":
            self.remote_known_hosts_policy = "strict"
            self._set_error("")
            self._clear_remote_validate_status()
            self._render_mode()
        elif event.button.id == "choose_project_dir":
            if self.workspace_source != "local":
                return
            self.app.push_screen(
                PathPickerScreen(
                    translator=self.translator,
                    initial_path=self.project_dir,
                ),
                self._on_project_dir_selected,
            )
        elif event.button.id == "choose_session_dir":
            initial_session_dir = (
                self.session_dir.resolve()
                if self.session_dir is not None and self.session_dir.is_dir()
                else self.sessions_dir
            )
            picker = PathPickerScreen(
                translator=self.translator,
                initial_path=initial_session_dir,
                root_path=self.sessions_dir,
                title_text=self.translator.text("session_picker_title"),
                selected_label=self.translator.text("selected_session_dir"),
                confirm_label=self.translator.text("select_session_dir"),
                session_picker=True,
            )
            self.app.push_screen(picker, self._on_session_dir_selected)
        elif event.button.id == "save_launch_config":
            await self._save_configuration()
        elif event.button.id == "cancel_launch_config":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "launch_remote_target_input":
            self.remote_target = event.value.strip()
            self._set_error("")
            self._clear_remote_validate_status()
        elif event.input.id == "launch_remote_dir_input":
            self.remote_dir = event.value.strip()
            self._set_error("")
            self._clear_remote_validate_status()
        elif event.input.id == "launch_remote_key_input":
            self.remote_key_path = event.value.strip()
            self._set_error("")
            self._clear_remote_validate_status()
        elif event.input.id == "launch_remote_password_input":
            self.remote_password = event.value
            self._set_error("")
            self._clear_remote_validate_status()

    def _on_project_dir_selected(self, project_dir: Path | None) -> None:
        if project_dir is None:
            return
        self.project_dir = project_dir.resolve()
        self.workspace_source = "local"
        self._render_project_dir()
        self._set_error("")
        self.dismiss(
            SessionLaunchConfig.create(
                self.project_dir,
                None,
                session_mode=self.session_mode,
                sandbox_backend=self.sandbox_backend,
            )
        )

    async def _save_configuration(self) -> None:
        if self.mode != "project":
            return
        if self.workspace_source != "remote":
            return
        if self.remote_validate_busy:
            return
        if self.session_mode != WorkspaceMode.DIRECT:
            self._set_error(self.translator.text("remote_requires_direct_mode"))
            return
        payload = {
            "kind": "remote_ssh",
            "ssh_target": self.remote_target,
            "remote_dir": self.remote_dir,
            "auth_mode": self.remote_auth_mode,
            "identity_file": self.remote_key_path,
            "known_hosts_policy": self.remote_known_hosts_policy,
            "remote_os": "linux",
        }
        if self.remote_auth_mode == "password":
            if not str(self.remote_password or "").strip():
                self._set_remote_validate_status(
                    self._format_remote_validate_failed(self.translator.text("remote_password_required")),
                    ok=False,
                )
                return
            payload["identity_file"] = ""
        try:
            remote = normalize_remote_session_config(payload)
        except ValueError as exc:
            self._set_remote_validate_status(
                self._format_remote_validate_failed(str(exc)),
                ok=False,
            )
            return
        remote_payload = {
            "kind": remote.kind,
            "ssh_target": remote.ssh_target,
            "remote_dir": remote.remote_dir,
            "auth_mode": remote.auth_mode,
            "identity_file": remote.identity_file,
            "known_hosts_policy": remote.known_hosts_policy,
            "remote_os": remote.remote_os,
        }
        remote_password = self.remote_password if self.remote_auth_mode == "password" else ""
        self.remote_validate_busy = True
        self._set_error("")
        self._set_remote_validate_status(self.translator.text("remote_validate_busy"), ok=None)
        self._render_mode()
        dismissed = False
        try:
            result = await self.app.validate_remote_workspace_config(  # type: ignore[attr-defined]
                remote=remote_payload,
                remote_password=remote_password,
                session_mode=WorkspaceMode.DIRECT.value,
                sandbox_backend=self.sandbox_backend,
            )
            if not bool((result or {}).get("ok")):
                reason = str((result or {}).get("stderr") or (result or {}).get("stdout") or "").strip()
                self._set_remote_validate_status(self._format_remote_validate_failed(reason), ok=False)
                return
            detail = self._summarize_remote_validate_output(str((result or {}).get("stderr") or ""))
            ok_text = (
                f"{self.translator.text('remote_validate_ok')} {detail}"
                if detail
                else self.translator.text("remote_validate_ok")
            )
            self._set_remote_validate_status(ok_text, ok=True)
            dismissed = True
            self.dismiss(
                SessionLaunchConfig.create(
                    None,
                    None,
                    session_mode=WorkspaceMode.DIRECT,
                    sandbox_backend=self.sandbox_backend,
                    remote=remote_payload,
                    remote_password=remote_password,
                )
            )
        except Exception as exc:
            self._set_remote_validate_status(self._format_remote_validate_failed(str(exc)), ok=False)
        finally:
            self.remote_validate_busy = False
            if not dismissed:
                self._render_mode()

    def _format_remote_validate_failed(self, reason: str) -> str:
        detail = str(reason or "").strip()
        if not detail:
            return self.translator.text("remote_validate_failed")
        prefixes = [
            f"{self.translator.text('remote_validate_failed')}:",
            f"{self.translator.text('remote_validate_failed')}：",
            "Remote validation failed:",
            "Remote validation failed：",
            "远程校验失败:",
            "远程校验失败：",
        ]
        for prefix in prefixes:
            if detail.startswith(prefix):
                detail = str(detail[len(prefix) :]).strip()
                break
        detail = self._summarize_remote_validate_output(detail)
        if not detail:
            return self.translator.text("remote_validate_failed")
        return f"{self.translator.text('remote_validate_failed')}: {detail}"

    def _summarize_remote_validate_output(
        self,
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
            kept = lines[:max_lines]
            kept.append("...")
            text = "\n".join(kept)
        return text

    def _normalize_sandbox_backend(self, backend: str | None, *, fallback: str | None = None) -> str:
        candidate = str(backend or "").strip().lower()
        if candidate in self.sandbox_backends:
            return candidate
        fallback_candidate = str(fallback or "").strip().lower()
        if fallback_candidate in self.sandbox_backends:
            return fallback_candidate
        return self.sandbox_backends[0]

    def _workspace_mode_locked(self) -> bool:
        return self.mode == "resume" and self.session_mode_locked

    def _render_project_dir(self) -> None:
        project_dir = self.project_dir or self.translator.text("unset_value")
        self.query_one("#launch_project_value", Static).update(
            f"{self.translator.text('selected_project_dir')}: {project_dir}"
        )

    def _render_session_dir(self) -> None:
        session_dir = self.session_dir or self.translator.text("unset_value")
        self.query_one("#launch_session_value", Static).update(
            f"{self.translator.text('selected_session_dir')}: {session_dir}"
        )

    def _resolve_initial_session_dir(self, session_id: str | None) -> Path | None:
        normalized = (session_id or "").strip()
        if not normalized:
            return None
        return (self.sessions_dir / normalized).resolve()

    def _on_session_dir_selected(self, session_dir: Path | None) -> None:
        if session_dir is None:
            return
        resolved = session_dir.resolve()
        if not resolved.is_dir() or resolved == self.sessions_dir or self.sessions_dir not in resolved.parents:
            self._set_error(self.translator.text("error_session_required"))
            return
        self.session_dir = resolved
        self._render_session_dir()
        self._set_error("")
        self.remote_validate_busy = True
        self._set_remote_validate_status(self.translator.text("remote_validate_busy"), ok=None)
        self._render_mode()
        asyncio.create_task(self._validate_and_apply_selected_session(resolved))

    async def _validate_and_apply_selected_session(self, session_dir: Path) -> None:
        dismissed = False
        try:
            if self.sandbox_backend == "anthropic":
                loaded_remote = load_remote_session_config(session_dir)
                if loaded_remote is not None:
                    remote_password = ""
                    if loaded_remote.auth_mode == "password" and str(loaded_remote.password_ref or "").strip():
                        with suppress(Exception):
                            remote_password = str(load_remote_session_password(loaded_remote.password_ref) or "").strip()
                    result = await self.app.validate_remote_workspace_config(  # type: ignore[attr-defined]
                        remote={
                            "kind": loaded_remote.kind,
                            "ssh_target": loaded_remote.ssh_target,
                            "remote_dir": loaded_remote.remote_dir,
                            "auth_mode": loaded_remote.auth_mode,
                            "identity_file": loaded_remote.identity_file,
                            "known_hosts_policy": loaded_remote.known_hosts_policy,
                            "remote_os": loaded_remote.remote_os,
                        },
                        remote_password=remote_password,
                        session_mode=WorkspaceMode.DIRECT.value,
                        sandbox_backend=self.sandbox_backend,
                    )
                    if not bool((result or {}).get("ok")):
                        reason = str((result or {}).get("stderr") or (result or {}).get("stdout") or "").strip()
                        self._set_remote_validate_status(self._format_remote_validate_failed(reason), ok=False)
                        return
            self.dismiss(
                SessionLaunchConfig.create(
                    None,
                    session_dir.name,
                    session_mode=self.session_mode,
                    session_mode_locked=self.session_mode_locked,
                    sandbox_backend=self.sandbox_backend,
                )
            )
            dismissed = True
        except Exception as exc:
            self._set_remote_validate_status(self._format_remote_validate_failed(str(exc)), ok=False)
        finally:
            self.remote_validate_busy = False
            if not dismissed:
                self._render_mode()

    def _render_mode(self) -> None:
        project_button = self.query_one("#launch_mode_project_button", Button)
        resume_button = self.query_one("#launch_mode_resume_button", Button)
        sandbox_anthropic_button = self.query_one("#launch_sandbox_backend_anthropic_button", Button)
        sandbox_none_button = self.query_one("#launch_sandbox_backend_none_button", Button)
        direct_button = self.query_one("#launch_workspace_mode_direct_button", Button)
        staged_button = self.query_one("#launch_workspace_mode_staged_button", Button)
        source_local_button = self.query_one("#launch_workspace_source_local_button", Button)
        source_remote_button = self.query_one("#launch_workspace_source_remote_button", Button)
        remote_auth_key_button = self.query_one("#launch_remote_auth_key_button", Button)
        remote_auth_password_button = self.query_one("#launch_remote_auth_password_button", Button)
        known_hosts_accept_new_button = self.query_one(
            "#launch_remote_known_hosts_accept_new_button", Button
        )
        known_hosts_strict_button = self.query_one("#launch_remote_known_hosts_strict_button", Button)
        remote_help = self.query_one("#launch_remote_help", Static)
        remote_target_label = self.query_one("#launch_remote_target_label", Static)
        remote_target_input = self.query_one("#launch_remote_target_input", Input)
        remote_dir_label = self.query_one("#launch_remote_dir_label", Static)
        remote_dir_input = self.query_one("#launch_remote_dir_input", Input)
        remote_key_row = self.query_one("#launch_remote_key_row", Vertical)
        remote_key_label = self.query_one("#launch_remote_key_label", Static)
        remote_key_input = self.query_one("#launch_remote_key_input", Input)
        remote_password_row = self.query_one("#launch_remote_password_row", Vertical)
        remote_password_label = self.query_one("#launch_remote_password_label", Static)
        remote_password_input = self.query_one("#launch_remote_password_input", Input)
        remote_validate_status = self.query_one("#launch_remote_validate_status", Static)
        resume_validate_status = self.query_one("#launch_resume_validate_status", Static)
        sandbox_status = self.query_one("#launch_sandbox_backend_status", Static)
        project_button.label = self.translator.text("setup_mode_project")
        resume_button.label = self.translator.text("setup_mode_session")
        sandbox_anthropic_button.label = self.translator.text("sandbox_backend_anthropic")
        sandbox_none_button.label = self.translator.text("sandbox_backend_none")
        direct_button.label = self.translator.text("workspace_mode_direct")
        staged_button.label = self.translator.text("workspace_mode_staged")
        source_local_button.label = self.translator.text("workspace_source_local")
        source_remote_button.label = self.translator.text("workspace_source_remote")
        remote_auth_key_button.label = self.translator.text("remote_auth_key")
        remote_auth_password_button.label = self.translator.text("remote_auth_password")
        known_hosts_accept_new_button.label = self.translator.text("remote_known_hosts_accept_new")
        known_hosts_strict_button.label = self.translator.text("remote_known_hosts_strict")
        remote_target_label.update(self.translator.text("remote_target_label"))
        remote_dir_label.update(self.translator.text("remote_dir_label"))
        remote_key_label.update(self.translator.text("remote_key_label"))
        remote_password_label.update(self.translator.text("remote_password_label"))
        remote_target_input.placeholder = self.translator.text("remote_target_placeholder")
        remote_dir_input.placeholder = self.translator.text("remote_dir_placeholder")
        remote_key_input.placeholder = self.translator.text("remote_key_placeholder")
        remote_password_input.placeholder = self.translator.text("remote_password_label")
        if remote_target_input.value != self.remote_target:
            remote_target_input.value = self.remote_target
        if remote_dir_input.value != self.remote_dir:
            remote_dir_input.value = self.remote_dir
        if remote_key_input.value != self.remote_key_path:
            remote_key_input.value = self.remote_key_path
        if remote_password_input.value != self.remote_password:
            remote_password_input.value = self.remote_password
        project_button.variant = "primary" if self.mode == "project" else "default"
        resume_button.variant = "primary" if self.mode == "resume" else "default"
        sandbox_anthropic_button.variant = (
            "primary" if self.sandbox_backend == "anthropic" else "default"
        )
        sandbox_none_button.variant = "primary" if self.sandbox_backend == "none" else "default"
        direct_button.variant = "primary" if self.session_mode == WorkspaceMode.DIRECT else "default"
        staged_button.variant = "primary" if self.session_mode == WorkspaceMode.STAGED else "default"
        source_local_button.variant = "primary" if self.workspace_source == "local" else "default"
        source_remote_button.variant = "primary" if self.workspace_source == "remote" else "default"
        remote_auth_key_button.variant = (
            "primary" if self.remote_auth_mode == "key" else "default"
        )
        remote_auth_password_button.variant = (
            "primary" if self.remote_auth_mode == "password" else "default"
        )
        known_hosts_accept_new_button.variant = (
            "primary" if self.remote_known_hosts_policy == "accept_new" else "default"
        )
        known_hosts_strict_button.variant = (
            "primary" if self.remote_known_hosts_policy == "strict" else "default"
        )
        ui_busy = self.remote_validate_busy
        workspace_mode_locked = self._workspace_mode_locked()
        workspace_mode_description = self.translator.text(
            f"workspace_mode_{self.session_mode.value}_desc"
        )
        sandbox_anthropic_button.display = "anthropic" in self.sandbox_backends
        sandbox_none_button.display = "none" in self.sandbox_backends
        sandbox_anthropic_button.disabled = ui_busy
        sandbox_none_button.disabled = ui_busy
        sandbox_status.update(
            f"{self.translator.text('sandbox_backend_label')}: "
            f"{self.translator.text(f'sandbox_backend_{self.sandbox_backend}')}\n"
            f"{self.translator.text(f'sandbox_backend_{self.sandbox_backend}_desc')}"
        )
        direct_button.disabled = workspace_mode_locked
        staged_button.disabled = workspace_mode_locked
        project_section = self.query_one("#launch_project_section", Vertical)
        project_local_section = self.query_one("#launch_project_local_section", Vertical)
        project_remote_section = self.query_one("#launch_project_remote_section", Vertical)
        resume_section = self.query_one("#launch_resume_section", Vertical)
        save_button = self.query_one("#save_launch_config", Button)
        mode_status = self.query_one("#launch_workspace_mode_status", Static)
        session_status = self.query_one("#launch_session_value", Static)
        locked_suffix = (
            f" ({self.translator.text('configuration_ready')})"
            if workspace_mode_locked
            else ""
        )
        mode_status.update(
            f"{self.translator.text('workspace_mode_label')}: "
            f"{self.translator.text(f'workspace_mode_{self.session_mode.value}')}"
            f"{locked_suffix}\n"
            f"{workspace_mode_description}"
        )
        if self.mode == "project":
            using_remote_source = self.workspace_source == "remote"
            source_remote_button.disabled = ui_busy or self.session_mode != WorkspaceMode.DIRECT
            source_local_button.disabled = ui_busy
            project_button.disabled = ui_busy
            resume_button.disabled = ui_busy
            direct_button.disabled = workspace_mode_locked or ui_busy
            staged_button.disabled = workspace_mode_locked or ui_busy
            self.query_one("#launch_mode_status", Static).update(
                self.translator.text("launch_mode_project_help")
            )
            self._render_project_dir()
            remote_help.update(
                (
                    f"{self.translator.text('remote_target_label')} / "
                    f"{self.translator.text('remote_dir_label')} / "
                    f"{self.translator.text('remote_known_hosts_label')}"
                )
            )
            remote_status_text = str(self.remote_validate_status or "").strip()
            if remote_status_text != self._remote_validate_status_rendered:
                remote_validate_status.update(remote_status_text)
                self._remote_validate_status_rendered = remote_status_text
            if self.remote_validate_ok is True:
                next_color = "#6ed49b"
            elif self.remote_validate_ok is False:
                next_color = "#ff6f86"
            else:
                next_color = "default"
            if next_color != self._remote_validate_status_color_rendered:
                remote_validate_status.styles.color = next_color
                self._remote_validate_status_color_rendered = next_color
            if self._resume_validate_status_rendered:
                resume_validate_status.update("")
                self._resume_validate_status_rendered = ""
            if self._resume_validate_status_color_rendered != "default":
                resume_validate_status.styles.color = "default"
                self._resume_validate_status_color_rendered = "default"
            project_section.display = True
            resume_section.display = False
            project_local_section.display = not using_remote_source
            project_remote_section.display = using_remote_source
            remote_key_row.display = using_remote_source and self.remote_auth_mode == "key"
            remote_password_row.display = using_remote_source and self.remote_auth_mode == "password"
            remote_target_input.disabled = ui_busy or not using_remote_source
            remote_dir_input.disabled = ui_busy or not using_remote_source
            remote_auth_key_button.disabled = ui_busy or not using_remote_source
            remote_auth_password_button.disabled = ui_busy or not using_remote_source
            remote_key_input.disabled = ui_busy or not using_remote_source or self.remote_auth_mode != "key"
            remote_password_input.disabled = (
                ui_busy or not using_remote_source or self.remote_auth_mode != "password"
            )
            known_hosts_accept_new_button.disabled = ui_busy or not using_remote_source
            known_hosts_strict_button.disabled = ui_busy or not using_remote_source
            save_button.display = using_remote_source
            save_button.label = self.translator.text("remote_validate_button")
            save_button.disabled = ui_busy
            self.query_one("#choose_project_dir", Button).disabled = ui_busy
            if not using_remote_source:
                choose_project_dir_button = self.query_one("#choose_project_dir", Button)
                if not choose_project_dir_button.has_focus:
                    choose_project_dir_button.focus()
        else:
            source_remote_button.disabled = True
            source_local_button.disabled = True
            project_button.disabled = False
            resume_button.disabled = False
            direct_button.disabled = workspace_mode_locked
            staged_button.disabled = workspace_mode_locked
            self.query_one("#launch_mode_status", Static).update(
                self.translator.text("launch_mode_session_help")
            )
            session_status.update(
                f"{self.translator.text('selected_session_dir')}: "
                f"{self.session_dir or self.translator.text('unset_value')}"
            )
            project_section.display = False
            resume_section.display = True
            if self._remote_validate_status_rendered:
                remote_validate_status.update("")
                self._remote_validate_status_rendered = ""
            if self._remote_validate_status_color_rendered != "default":
                remote_validate_status.styles.color = "default"
                self._remote_validate_status_color_rendered = "default"
            resume_status_text = str(self.remote_validate_status or "").strip()
            if resume_status_text != self._resume_validate_status_rendered:
                resume_validate_status.update(resume_status_text)
                self._resume_validate_status_rendered = resume_status_text
            if self.remote_validate_ok is True:
                next_resume_color = "#6ed49b"
            elif self.remote_validate_ok is False:
                next_resume_color = "#ff6f86"
            else:
                next_resume_color = "default"
            if next_resume_color != self._resume_validate_status_color_rendered:
                resume_validate_status.styles.color = next_resume_color
                self._resume_validate_status_color_rendered = next_resume_color
            remote_key_row.display = False
            remote_password_row.display = False
            remote_target_input.disabled = True
            remote_dir_input.disabled = True
            remote_auth_key_button.disabled = True
            remote_auth_password_button.disabled = True
            remote_key_input.disabled = True
            remote_password_input.disabled = True
            known_hosts_accept_new_button.disabled = True
            known_hosts_strict_button.disabled = True
            save_button.display = False
            choose_session_dir_button = self.query_one("#choose_session_dir", Button)
            if not choose_session_dir_button.has_focus:
                choose_session_dir_button.focus()

    def _set_error(self, message: str) -> None:
        self.query_one("#launch_config_error", Static).update(message)

    def _set_remote_validate_status(self, message: str, *, ok: bool | None) -> None:
        self.remote_validate_status = str(message or "").strip()
        self.remote_validate_ok = ok

    def _clear_remote_validate_status(self) -> None:
        self._set_remote_validate_status("", ok=None)


class ToolRunDetailScreen(ModalScreen[None]):
    CSS = """
    #tool_run_detail_dialog {
        width: 92%;
        height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #tool_run_detail_title {
        height: auto;
        text-style: bold;
        padding: 0 0 1 0;
    }

    #tool_run_detail_scroll {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }

    #tool_run_detail_body {
        width: 100%;
        height: auto;
        padding: 0 0 1 0;
    }

    #tool_run_detail_actions {
        height: auto;
        margin-top: 1;
    }

    #tool_run_detail_actions Button {
        width: auto;
        min-width: 0;
    }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        *,
        translator: Translator,
        run_id: str,
        run_snapshot: dict[str, Any] | None,
        timeline_entries: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self.translator = translator
        self.run_id = run_id
        self.run_snapshot = run_snapshot if isinstance(run_snapshot, dict) else None
        self.timeline_entries = [
            entry
            for entry in timeline_entries
            if isinstance(entry, dict)
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="tool_run_detail_dialog"):
            yield Static("", id="tool_run_detail_title")
            with VerticalScroll(id="tool_run_detail_scroll"):
                yield Static("", id="tool_run_detail_body")
            with Horizontal(id="tool_run_detail_actions"):
                yield Button("", id="tool_run_detail_close_button", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#tool_run_detail_close_button", Button).label = self.translator.text(
            "tool_runs_detail_close"
        )
        self._render_detail()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tool_run_detail_close_button":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)

    def refresh_detail(
        self,
        *,
        translator: Translator | None = None,
        run_id: str | None = None,
        run_snapshot: dict[str, Any] | None = None,
        timeline_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        if translator is not None:
            self.translator = translator
        if run_id is not None:
            self.run_id = str(run_id).strip()
        if run_snapshot is not None or self.run_snapshot is None:
            self.run_snapshot = run_snapshot if isinstance(run_snapshot, dict) else None
        if timeline_entries is not None:
            self.timeline_entries = [
                entry
                for entry in timeline_entries
                if isinstance(entry, dict)
            ]
        close_button = self.query_one("#tool_run_detail_close_button", Button)
        close_button.label = self.translator.text("tool_runs_detail_close")
        self._render_detail()

    def _render_detail(self) -> None:
        title = self.query_one("#tool_run_detail_title", Static)
        body = self.query_one("#tool_run_detail_body", Static)
        run_id = self.run_id or self.translator.text("none_value")
        title.update(f"{self.translator.text('tool_runs_detail_title')} · {run_id}")
        body.update(self._detail_text())

    def _detail_text(self) -> Text:
        text = Text()
        run = self.run_snapshot if isinstance(self.run_snapshot, dict) else None
        if run is None:
            text.append(self.translator.text("none_value"), style="dim")
            return text

        self._append_detail_heading(text, self.translator.text("tool_runs_detail_overview"))
        rows = [
            (self.translator.text("session_status"), str(run.get("status", "-"))),
            (self.translator.text("session_id"), str(run.get("session_id", "-"))),
            (self.translator.text("agents"), str(run.get("agent_id", "-"))),
            (self.translator.text("tool_runs_group_tool"), str(run.get("tool_name", "-"))),
            (
                self.translator.text("tool_runs_duration"),
                self._duration_text(tool_run_duration_ms(run)),
            ),
            (
                self.translator.text("tool_runs_detail_parent_run"),
                str(run.get("parent_run_id") or self.translator.text("none_value")),
            ),
            (
                self.translator.text("tool_runs_detail_created_at"),
                self._timestamp_text(run.get("created_at")),
            ),
            (
                self.translator.text("tool_runs_detail_started_at"),
                self._timestamp_text(run.get("started_at")),
            ),
            (
                self.translator.text("tool_runs_detail_completed_at"),
                self._timestamp_text(run.get("completed_at")),
            ),
        ]
        for key, value in rows:
            text.append(f"{key}: ", style="bold cyan")
            text.append(f"{value}\n")

        text.append("\n")
        self._append_detail_heading(text, self.translator.text("tool_runs_detail_arguments"))
        text.append(self._format_structured_value(run.get("arguments"), fallback=self.translator.text("none_value")))
        text.append("\n\n")

        self._append_detail_heading(text, self.translator.text("tool_runs_detail_result"))
        text.append(
            self._format_structured_value(
                run.get("result"),
                fallback=self.translator.text("tool_runs_detail_no_result"),
            )
        )

        run_error = str(run.get("error", "")).strip()
        if run_error:
            text.append("\n\n")
            self._append_detail_heading(text, self.translator.text("tool_runs_detail_error"))
            text.append(run_error, style="red")

        text.append("\n\n")
        self._append_detail_heading(text, self.translator.text("tool_runs_detail_timeline"))
        if not self.timeline_entries:
            text.append(self.translator.text("tool_runs_detail_no_timeline"), style="dim")
            return text

        for entry in self.timeline_entries:
            event_type = str(entry.get("event_type", "-"))
            summary = self._timeline_entry_summary(entry)
            timestamp = self._timestamp_text(entry.get("timestamp"))
            text.append(f"- {event_type} | {summary} | {timestamp}\n", style="bold")
            payload = entry.get("payload")
            payload_text = self._format_structured_value(payload, fallback=self.translator.text("none_value"))
            payload_lines = payload_text.splitlines() or [payload_text]
            for line in payload_lines:
                text.append(f"  {line}\n", style="dim")
        return text

    @staticmethod
    def _append_detail_heading(target: Text, heading: str) -> None:
        target.append(f"{heading}\n", style="bold yellow")

    @staticmethod
    def _timestamp_text(value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or "-"

    @staticmethod
    def _duration_text(duration_ms: Any) -> str:
        try:
            value = int(duration_ms)
        except (TypeError, ValueError):
            return "-"
        if value < 1_000:
            return f"{value}ms"
        seconds = value / 1_000
        if seconds < 60:
            precision = 2 if seconds < 10 else 1
            return f"{seconds:.{precision}f}s"
        minutes = int(seconds // 60)
        remaining = int(round(seconds % 60))
        return f"{minutes}m{remaining}s"

    @classmethod
    def _format_structured_value(cls, value: Any, *, fallback: str) -> str:
        if value is None:
            return fallback
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return fallback
            parsed = cls._parse_json_like_text(normalized)
            if parsed is not None:
                with suppress(TypeError, ValueError):
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
            return normalized
        with suppress(TypeError, ValueError):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _parse_json_like_text(text: str) -> Any | None:
        normalized = text.strip()
        if not normalized:
            return None
        if not (
            (normalized.startswith("{") and normalized.endswith("}"))
            or (normalized.startswith("[") and normalized.endswith("]"))
        ):
            return None
        with suppress(json.JSONDecodeError):
            return json.loads(normalized)
        return None

    def _timeline_entry_summary(self, entry: dict[str, Any]) -> str:
        event_type = str(entry.get("event_type", "")).strip()
        payload = entry.get("payload")
        details = payload if isinstance(payload, dict) else {}
        if event_type == "tool_call_started":
            action = details.get("action")
            if isinstance(action, dict):
                return str(action.get("type", "tool"))
            return "tool"
        if event_type == "tool_call":
            action = details.get("action")
            if isinstance(action, dict):
                action_name = str(action.get("type", "tool"))
            else:
                action_name = "tool"
            result = details.get("result")
            result_type = result.__class__.__name__ if result is not None else "none"
            return f"{action_name} -> {result_type}"
        if event_type == "tool_run_submitted":
            return (
                f"id={str(details.get('tool_run_id', '-'))} "
                f"tool={str(details.get('tool_name', '-'))}"
            )
        if event_type == "tool_run_updated":
            return (
                f"id={str(details.get('tool_run_id', '-'))} "
                f"status={str(details.get('status', '-'))}"
            )
        return event_type or "-"


class SteerInputScreen(ModalScreen[str | None]):
    CSS = """
    #steer_input_dialog {
        width: 86%;
        height: auto;
        max-height: 86%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #steer_input_title {
        text-style: bold;
        margin-bottom: 1;
    }

    #steer_input_value {
        height: 10;
        border: round $accent;
    }

    #steer_input_error {
        min-height: 1;
        color: $error;
        margin-top: 1;
    }

    #steer_input_buttons {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, translator: Translator, agent_name: str) -> None:
        super().__init__()
        self.translator = translator
        self.agent_name = str(agent_name or "").strip()

    def compose(self) -> ComposeResult:
        title = self.translator.text("steer_input_prompt")
        if self.agent_name:
            title = f"{title} {self.agent_name}"
        with Vertical(id="steer_input_dialog"):
            yield Static(title, id="steer_input_title")
            steer_input = TextArea("", id="steer_input_value")
            steer_input.border_title = self.translator.text("steer_button")
            yield steer_input
            yield Static("", id="steer_input_error")
            with Horizontal(id="steer_input_buttons"):
                yield Button(
                    self.translator.text("steer_button"),
                    id="steer_input_confirm",
                    variant="primary",
                )
                yield Button(self.translator.text("cancel"), id="steer_input_cancel")

    def on_mount(self) -> None:
        self.query_one("#steer_input_value", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "steer_input_confirm":
            self._submit()
        elif event.button.id == "steer_input_cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        text_area = self.query_one("#steer_input_value", TextArea)
        content = text_area.text.strip()
        if not content:
            self.query_one("#steer_input_error", Static).update(
                self.translator.text("steer_input_required")
            )
            return
        self.dismiss(content)


class OpenCompanyApp(App):
    CSS = """
    #layout {
        height: 1fr;
    }

    #status_panel {
        height: auto;
        margin: 0 1;
    }

    #controls {
        height: auto;
        padding: 0 1;
    }

    #control_row {
        height: auto;
        layout: vertical;
    }

    #model_row {
        height: auto;
    }

    #model_input_row {
        width: auto;
        min-width: 30;
        max-width: 72;
        height: auto;
    }

    #model_label {
        width: 7;
        content-align: left middle;
    }

    #model_input {
        width: 42;
        min-width: 20;
        max-width: 60;
        margin-right: 1;
    }

    #root_agent_name_input_row {
        width: auto;
        min-width: 36;
        max-width: 84;
        height: auto;
    }

    #root_agent_name_label {
        width: 16;
        content-align: left middle;
    }

    #root_agent_name_input {
        width: 40;
        min-width: 20;
        max-width: 60;
        margin-right: 1;
    }

    #locale_controls {
        width: auto;
        height: auto;
    }

    #locale_label {
        width: 6;
        content-align: left middle;
    }

    #locale_controls Button {
        width: 8;
        min-width: 6;
        padding: 0 1;
    }

    #task_row {
        width: 100%;
        height: auto;
    }

    #task_label {
        width: 7;
        content-align: left middle;
    }

    #task_input {
        width: 1fr;
        max-width: 100;
        height: 3;
        min-height: 3;
        max-height: 9;
    }

    #buttons {
        height: auto;
        width: 100%;
    }

    #buttons Button,
    #diff_actions Button {
        width: auto;
        min-width: 0;
    }

    #buttons Button {
        min-width: 10;
        margin: 0 1 0 0;
    }

    #main_tabs {
        height: 1fr;
        margin: 0 1;
    }

    #monitor_tab {
        height: 1fr;
    }

    #agents_tab {
        height: 1fr;
    }

    #monitor_body,
    #agents_tab_body {
        height: 1fr;
    }

    #overview_tree {
        height: 1fr;
        min-height: 5;
        width: 100%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }

    #overview_scroll {
        height: 1fr;
    }

    #live_tree {
        height: 1fr;
        width: 100%;
        padding: 0;
    }

    #live_scroll {
        height: 1fr;
    }

    #overview_content {
        width: 100%;
        height: auto;
        padding: 0 1 1 1;
    }

    #live_content {
        width: 100%;
        height: auto;
        padding: 0 0 1 0;
    }

    #activity_log {
        height: 1fr;
        width: 100%;
        min-height: 4;
        margin-top: 1;
        border: round $accent;
    }

    .panel-title {
        height: auto;
        padding: 0 1;
        text-style: bold;
    }

    .empty-state {
        color: $text-muted;
        padding: 1;
    }

    .agent-collapsible {
        width: 1fr;
        height: auto;
        background: transparent;
        border-top: none;
        padding: 0 0 1 0;
    }

    .live-agent-card {
        width: 100%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
        margin-bottom: 1;
    }

    .agent-collapsible > CollapsibleTitle {
        padding: 0 1;
    }

    .agent-collapsible > Contents {
        width: 100%;
        height: auto;
        padding: 1 0 0 2;
    }

    .agent-detail {
        width: 100%;
        padding: 0 1;
    }

    .agent-detail.non-message-stream {
        border: dashed $accent;
    }

    .agent-jump-row {
        height: auto;
        width: 100%;
        padding: 0 1 1 1;
    }

    .agent-jump-button {
        width: auto;
        min-width: 0;
    }

    .agent-terminate-button {
        color: $error;
    }

    .live-step-body {
        width: 100%;
        height: auto;
    }

    .agent-section-collapsible,
    .live-step-collapsible {
        width: 1fr;
        height: auto;
        border-top: none;
        background: transparent;
        padding: 0 0 1 0;
    }

    .agent-section-collapsible > Contents,
    .live-step-collapsible > Contents {
        width: 100%;
        height: auto;
        padding: 1 0 0 2;
    }

    #diff_tab_body {
        height: 1fr;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }

    #diff_panel_title {
        height: auto;
        padding: 0 1;
        text-style: bold;
    }

    #diff_scroll {
        height: 1fr;
    }

    #diff_content {
        width: 100%;
        height: auto;
        padding: 0 1 1 1;
    }

    #diff_actions {
        height: auto;
        padding: 0 1 1 1;
    }

    #tool_runs_tab_body {
        height: 1fr;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }

    #tool_runs_panel_title {
        height: auto;
        padding: 0 1;
        text-style: bold;
    }

    #tool_runs_actions {
        height: auto;
        padding: 0 1 1 1;
    }

    #tool_runs_actions Button {
        width: auto;
        min-width: 0;
    }

    #tool_runs_status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #tool_runs_scroll {
        height: 1fr;
    }

    #tool_runs_content {
        width: 100%;
        height: auto;
        padding: 0 1 1 1;
    }

    #steer_runs_tab_body {
        height: 1fr;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }

    #steer_runs_panel_title {
        height: auto;
        padding: 0 1;
        text-style: bold;
    }

    #steer_runs_actions {
        height: auto;
        padding: 0 1 1 1;
    }

    #steer_runs_actions Button {
        width: auto;
        min-width: 0;
    }

    #steer_runs_status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #steer_runs_scroll {
        height: 1fr;
    }

    #steer_runs_content {
        width: 100%;
        height: auto;
        padding: 0 1 1 1;
    }

    .steer-run-group {
        width: 1fr;
        height: auto;
        border: round $accent;
        padding: 0 1 1 1;
        margin-bottom: 1;
    }

    .steer-run-row {
        width: 1fr;
        height: auto;
        padding: 0 0 1 0;
    }

    .steer-run-card-header {
        height: auto;
        text-style: bold;
    }

    .steer-run-card-meta {
        height: auto;
        color: $text-muted;
        padding-bottom: 1;
    }

    .steer-run-card-content {
        height: auto;
        padding: 0 0 0 2;
    }

    .steer-run-card-actions {
        height: auto;
        padding-left: 2;
    }

    #config_tab_body {
        height: 1fr;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }

    #config_panel_title {
        height: auto;
        padding: 0 1;
        text-style: bold;
    }

    #config_path {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #config_sync_status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #config_effect_notes {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #config_editor {
        height: 1fr;
        margin-top: 1;
        border: round $accent;
    }

    #config_actions {
        height: auto;
        padding: 1 1 1 1;
    }
    """

    BINDINGS = [("ctrl+c", "interrupt_session", "Interrupt")]

    def __init__(
        self,
        *,
        project_dir: Path | None = None,
        session_id: str | None = None,
        session_mode: WorkspaceMode | str | None = None,
        remote_config: dict[str, Any] | RemoteSessionConfig | None = None,
        remote_password: str | None = None,
        app_dir: Path | None = None,
        locale: str | None = None,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.project_dir = project_dir.resolve() if project_dir else None
        self.configured_resume_session_id = (session_id or "").strip() or None
        self.session_mode = normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value)
        self._sandbox_backends = self._available_sandbox_backends()
        self.sandbox_backend_default = self._default_sandbox_backend_from_config()
        self.sandbox_backend = self._normalize_sandbox_backend(self.sandbox_backend_default)
        self.remote_config: RemoteSessionConfig | None = (
            normalize_remote_session_config(remote_config)
            if isinstance(remote_config, (dict, RemoteSessionConfig)) and remote_config
            else None
        )
        if self.remote_config is not None and self.session_mode != WorkspaceMode.DIRECT:
            raise ValueError("Remote workspace is supported only in direct mode.")
        if self.remote_config is not None:
            self.project_dir = None
        self.remote_password = str(remote_password or "").strip()
        self.session_mode_locked = False
        self.app_dir = app_dir.resolve() if app_dir else None
        self.orchestrator: Orchestrator | None = None
        self._locale_override: str | None = locale if locale in {"en", "zh"} else None
        self.locale = self._resolve_configured_locale(self._locale_override)
        self.debug_enabled = bool(debug)
        self.translator = Translator(self.locale)
        self.default_keep_pinned_messages = self._default_keep_pinned_messages_from_config()
        self.session_task: asyncio.Task | None = None
        self.agent_states: dict[str, AgentRuntimeView] = {}
        self.stream_agent_order: list[str] = []
        self.overview_collapsed_agent_ids: set[str] = set()
        self.overview_instruction_collapsed_agent_ids: set[str] = set()
        self.overview_summary_expanded_agent_ids: set[str] = set()
        self.live_collapsed_agent_ids: set[str] = set()
        self.live_step_collapsed_overrides: dict[tuple[str, int], bool] = {}
        self.current_session_id: str | None = None
        self.current_task: str = ""
        self.current_session_status: str = "idle"
        self.current_focus_agent_id: str | None = None
        self.current_summary: str = ""
        self.project_sync_state: dict[str, Any] | None = None
        self.project_sync_action_in_progress: bool = False
        self.last_project_sync_operation: dict[str, Any] | None = None
        self.status_message: str = self.translator.text("ready")
        self._task_input_seeded_default: str | None = None
        self._task_input_programmatic_update: bool = False
        self.selected_model: str = self._default_model_from_config()
        self._diagnostics: DiagnosticLogger | None = None
        self._panel_refresh_scheduled: bool = False
        self._panel_refresh_dirty: bool = False
        self._panel_refresh_interval_seconds: float = 0.10
        self._last_panel_refresh_time: float = 0.0
        self._diff_preview_cache_key: str | None = None
        self._diff_preview_cache_data: dict[str, Any] | None = None
        self._diff_render_cache_key: tuple[Any, ...] | None = None
        self._overview_structure_signature: tuple[Any, ...] | None = None
        self._live_structure_signature: tuple[Any, ...] | None = None
        self._overview_render_fingerprints: dict[str, tuple[Any, ...]] = {}
        self._live_agent_render_fingerprints: dict[str, tuple[Any, ...]] = {}
        self._live_step_render_fingerprints: dict[tuple[str, int], tuple[Any, ...]] = {}
        self._live_step_entry_render_fingerprints: dict[tuple[str, int, int], tuple[Any, ...]] = {}
        self._status_panel_cache_text: str = ""
        self._static_text_dirty: bool = True
        self._config_editor_updating: bool = False
        self._config_editor_dirty: bool = False
        self._config_last_saved_text: str = ""
        self._config_last_mtime_ns: int | None = None
        self._config_pending_external_mtime_ns: int | None = None
        self._config_notice_key: str = "config_sync_clean"
        self._config_notice_detail: str = ""
        self.tool_runs_filter: str = "all"
        self.tool_runs_group_by: str = "agent"
        self.tool_runs_snapshot: dict[str, Any] | None = None
        self.tool_runs_metrics_snapshot: dict[str, Any] | None = None
        self.tool_runs_status_message: str = ""
        self.tool_runs_selected_run_id: str | None = None
        self._tool_runs_dirty: bool = True
        self._tool_runs_cache_key: tuple[Any, ...] | None = None
        self._tool_run_call_id_to_run_id: dict[str, str] = {}
        self._tool_run_timeline_by_run_id: dict[str, list[dict[str, Any]]] = {}
        self._tool_runs_detail_open_run_id: str | None = None
        self._tool_runs_detail_run_snapshot: dict[str, Any] | None = None
        self.steer_runs_filter: str = "all"
        self.steer_runs_group_by: str = "agent"
        self.steer_runs_snapshot: dict[str, Any] | None = None
        self.steer_runs_metrics_snapshot: dict[str, Any] | None = None
        self.steer_runs_status_message: str = ""
        self._steer_runs_dirty: bool = True
        self._steer_runs_cache_key: tuple[Any, ...] | None = None
        self._message_cursor: str | None = None
        self._history_replay_in_progress: bool = False

    def _create_orchestrator(
        self,
        project_dir: Path,
        *,
        locale: str | None,
        app_dir: Path | None,
    ) -> Orchestrator:
        if self.debug_enabled:
            orchestrator = Orchestrator(project_dir, locale=locale, app_dir=app_dir, debug=True)
        else:
            orchestrator = Orchestrator(project_dir, locale=locale, app_dir=app_dir)
        self._apply_sandbox_backend(orchestrator)
        return orchestrator

    async def validate_remote_workspace_config(
        self,
        *,
        remote: dict[str, Any],
        remote_password: str | None = None,
        session_mode: WorkspaceMode | str | None = None,
        sandbox_backend: str | None = None,
    ) -> dict[str, Any]:
        normalized_mode = normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value)
        if normalized_mode != WorkspaceMode.DIRECT:
            raise ValueError(self.translator.text("remote_requires_direct_mode"))
        normalized_remote = normalize_remote_session_config(remote)
        password = str(remote_password or "").strip()
        if normalized_remote.auth_mode == "password" and not password:
            raise ValueError(self.translator.text("remote_password_required"))

        session_id = f"remote-validate-{uuid.uuid4().hex[:12]}"
        orchestrator = self._create_orchestrator(
            self.project_dir or Path.cwd(),
            locale=self.locale,
            app_dir=self.app_dir,
        )
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="layout"):
            yield Static("", id="status_panel")
            with Vertical(id="controls"):
                with Vertical(id="control_row"):
                    with Horizontal(id="model_row"):
                        with Horizontal(id="model_input_row"):
                            yield Static("", id="model_label")
                            yield Input("", id="model_input")
                        with Horizontal(id="root_agent_name_input_row"):
                            yield Static("", id="root_agent_name_label")
                            yield Input("", id="root_agent_name_input")
                        with Horizontal(id="locale_controls"):
                            yield Static("", id="locale_label")
                            yield Button("", id="locale_en_button")
                            yield Button("", id="locale_zh_button")
                    with Horizontal(id="task_row"):
                        yield Static("", id="task_label")
                        task_input = TextArea("", id="task_input")
                        task_input.border_title = self.translator.text("task_input")
                        yield task_input
                    with Horizontal(id="buttons"):
                        yield Button(self.translator.text("run"), id="run_button", variant="primary")
                        yield Button(self.translator.text("terminal"), id="terminal_button")
                        yield Button(self.translator.text("reconfigure"), id="reconfigure_button")
                        yield Button(self.translator.text("interrupt"), id="interrupt_button", variant="warning")
            with TabbedContent(id="main_tabs"):
                with TabPane(self.translator.text("monitor_tab_title"), id="monitor_tab"):
                    with Vertical(id="monitor_body"):
                        with Vertical(id="overview_tree"):
                            yield Static("", id="overview_panel_title", classes="panel-title")
                            with VerticalScroll(id="overview_scroll"):
                                yield Vertical(id="overview_content")
                        yield RichLog(id="activity_log", wrap=True, highlight=False)
                with TabPane(self.translator.text("agents_tab_title"), id="agents_tab"):
                    with Vertical(id="agents_tab_body"):
                        with Vertical(id="live_tree"):
                            yield Static("", id="live_panel_title", classes="panel-title")
                            with VerticalScroll(id="live_scroll"):
                                yield Vertical(id="live_content")
                with TabPane(self.translator.text("tool_runs_tab_title"), id="tool_runs_tab"):
                    with Vertical(id="tool_runs_tab_body"):
                        yield Static("", id="tool_runs_panel_title", classes="panel-title")
                        with Horizontal(id="tool_runs_actions"):
                            yield Button("", id="tool_runs_refresh_button", variant="primary")
                            yield Button("", id="tool_runs_filter_button")
                            yield Button("", id="tool_runs_group_button")
                            yield Button("", id="tool_runs_prev_button")
                            yield Button("", id="tool_runs_next_button")
                            yield Button("", id="tool_runs_detail_button")
                        yield Static("", id="tool_runs_status")
                        with VerticalScroll(id="tool_runs_scroll"):
                            yield Static("", id="tool_runs_content")
                with TabPane(self.translator.text("steer_runs_tab_title"), id="steer_runs_tab"):
                    with Vertical(id="steer_runs_tab_body"):
                        yield Static("", id="steer_runs_panel_title", classes="panel-title")
                        with Horizontal(id="steer_runs_actions"):
                            yield Button("", id="steer_runs_refresh_button", variant="primary")
                            yield Button("", id="steer_runs_filter_button")
                            yield Button("", id="steer_runs_group_button")
                        yield Static("", id="steer_runs_status")
                        with VerticalScroll(id="steer_runs_scroll"):
                            yield Vertical(id="steer_runs_content")
                with TabPane(self.translator.text("diff_tab_title"), id="diff_tab"):
                    with Vertical(id="diff_tab_body"):
                        yield Static("", id="diff_panel_title", classes="panel-title")
                        with VerticalScroll(id="diff_scroll"):
                            yield Static("", id="diff_content")
                        with Horizontal(id="diff_actions"):
                            yield Button(self.translator.text("apply"), id="apply_button", variant="primary")
                            yield Button(self.translator.text("undo"), id="undo_button")
                with TabPane(self.translator.text("config_tab_title"), id="config_tab"):
                    with Vertical(id="config_tab_body"):
                        yield Static("", id="config_panel_title", classes="panel-title")
                        yield Static("", id="config_path")
                        yield Static("", id="config_sync_status")
                        yield Static("", id="config_effect_notes")
                        yield TextArea("", id="config_editor")
                        with Horizontal(id="config_actions"):
                            yield Button(self.translator.text("save"), id="config_save_button", variant="primary")
                            yield Button(self.translator.text("reload"), id="config_reload_button")
        yield Footer()

    def on_mount(self) -> None:
        self._log_diagnostic("mounted")
        self._apply_responsive_layout()
        self._update_task_input_height()
        self._ensure_model_input_value()
        self._refresh_project_sync_state()
        self._reload_config_from_disk(force=True, source="initial")
        self.set_interval(1.0, self._poll_external_config_changes)
        self._render_all()
        self._set_controls_running(False)
        if self.configured_resume_session_id:
            self._restore_session_history(self.configured_resume_session_id)
        if not self._launch_config().can_run():
            self._update_status(self.translator.text("configuration_required"))
            self._open_launch_config()

    def _handle_exception(self, error: Exception) -> None:  # type: ignore[override]
        self._log_diagnostic("ui_exception", level="error", error=error)
        try:
            if self.orchestrator is not None:
                self.orchestrator.request_interrupt()
            self.current_session_status = "failed"
            self.current_summary = str(error)
            self._update_status(f"{self.translator.text('session_failed')}: {error}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] Fatal UI error: {error}")
            self._finalize_session_ui()
        except Exception:
            return

    async def on_unmount(self) -> None:
        active = bool(self.session_task and not self.session_task.done())
        self._log_diagnostic("unmounted", payload={"active_session": active})
        await self._cancel_active_session_task()

    async def _on_exit_app(self) -> None:  # type: ignore[override]
        if self._has_running_session():
            self._log_diagnostic("exit_app_blocked_running_session", level="warning")
            self._update_status("Session is running. Interrupt it first before quitting.")
            self.action_interrupt_session()
            return
        self._log_diagnostic("exit_app_allowed")
        await super()._on_exit_app()

    def exit(  # type: ignore[override]
        self,
        result: object | None = None,
        return_code: int = 0,
        message: object | None = None,
    ) -> None:
        if self._has_running_session():
            self._log_diagnostic("exit_blocked_running_session", level="warning")
            self._update_status("Session is running. Interrupt it first before quitting.")
            self.action_interrupt_session()
            return
        self._log_diagnostic("exit_allowed")
        super().exit(result=result, return_code=return_code, message=message)

    async def action_quit(self) -> None:
        if self._has_running_session():
            self._log_diagnostic("quit_blocked_running_session", level="warning")
            self._update_status("Session is running. Interrupt it first before quitting.")
            self.action_interrupt_session()
            return
        self._log_diagnostic("quit_allowed")
        await super().action_quit()

    def on_resize(self, event: events.Resize) -> None:
        del event
        if not self.query("#control_row"):
            return
        self._apply_responsive_layout()
        self._update_task_input_height()
        self._render_all()

    def action_interrupt_session(self) -> None:
        if self.orchestrator and self.session_task and not self.session_task.done():
            self._log_diagnostic("interrupt_requested", payload={"source": "ui"})
            self.orchestrator.request_interrupt()
            self.current_session_status = "interrupting"
            self._update_status(self.translator.text("interrupted"))
            self._render_all()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, AgentJumpButton):
            self._jump_to_agent(event.button.target_agent_id)
            return
        if isinstance(event.button, AgentSteerButton):
            await self._open_agent_steer_input(event.button.target_agent_id)
            return
        if isinstance(event.button, AgentTerminateButton):
            await self._terminate_agent_from_live_panel(event.button.target_agent_id)
            return
        if isinstance(event.button, AgentCopyButton):
            self._copy_agent_field_from_live_panel(event.button.copy_value, event.button.copy_kind)
            return
        if isinstance(event.button, SteerRunCancelButton):
            await self._cancel_steer_run_from_panel(event.button.steer_run_id)
            return
        if event.button.id not in MAIN_CONTROL_BUTTON_IDS:
            return
        if event.button.id == "locale_en_button":
            self._set_locale("en")
        elif event.button.id == "locale_zh_button":
            self._set_locale("zh")
        elif event.button.id == "run_button":
            await self._start_run()
        elif event.button.id == "terminal_button":
            self._open_terminal()
        elif event.button.id == "apply_button":
            await self._apply_project_sync()
        elif event.button.id == "undo_button":
            await self._undo_project_sync()
        elif event.button.id == "reconfigure_button":
            self._open_launch_config()
        elif event.button.id == "interrupt_button":
            self.action_interrupt_session()
        elif event.button.id == "tool_runs_refresh_button":
            self._tool_runs_dirty = True
            self._render_tool_runs_panel()
        elif event.button.id == "tool_runs_filter_button":
            self._cycle_tool_runs_filter()
            self._tool_runs_dirty = True
            self._render_tool_runs_panel()
        elif event.button.id == "tool_runs_group_button":
            self._cycle_tool_runs_group()
            self._render_tool_runs_panel()
        elif event.button.id == "tool_runs_prev_button":
            self._select_tool_run_relative(-1)
            self._render_tool_runs_panel()
        elif event.button.id == "tool_runs_next_button":
            self._select_tool_run_relative(1)
            self._render_tool_runs_panel()
        elif event.button.id == "tool_runs_detail_button":
            self._open_selected_tool_run_detail()
        elif event.button.id == "steer_runs_refresh_button":
            self._steer_runs_dirty = True
            self._render_steer_runs_panel()
        elif event.button.id == "steer_runs_filter_button":
            self._cycle_steer_runs_filter()
            self._steer_runs_dirty = True
            self._render_steer_runs_panel()
        elif event.button.id == "steer_runs_group_button":
            self._cycle_steer_runs_group()
            self._render_steer_runs_panel()
        elif event.button.id == "config_save_button":
            self._save_config_from_editor()
        elif event.button.id == "config_reload_button":
            self._reload_config_from_disk(force=True, source="manual")

    def _jump_to_agent(self, target_agent_id: str) -> None:
        target = self.agent_states.get(target_agent_id)
        if target is None:
            self._update_status(f"{self.translator.text('current_focus')}: {target_agent_id}")
            return
        self.current_focus_agent_id = target_agent_id
        self.live_collapsed_agent_ids.discard(target_agent_id)
        tabs = self._query_optional("#main_tabs", TabbedContent)
        if tabs is not None:
            with suppress(Exception):
                tabs.active = "agents_tab"
        self._update_status(f"{self.translator.text('current_focus')}: {target.name}")
        self._queue_agent_panel_refresh()
        self.call_later(lambda: self._scroll_to_live_agent(target_agent_id))

    def _scroll_to_live_agent(self, target_agent_id: str) -> None:
        agent_key = self._widget_safe_id(target_agent_id)
        widget = self._query_optional(f"#live-agent-{agent_key}", AgentCollapsible)
        if widget is None:
            return
        widget.collapsed = False
        with suppress(Exception):
            widget.scroll_visible(top=True, animate=False)

    def _copy_agent_field_from_live_panel(self, value: str, copy_kind: str) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            self._update_status(self.translator.text("copy_failed"))
            return
        try:
            self.copy_to_clipboard(normalized)
        except Exception:
            self._update_status(self.translator.text("copy_failed"))
            return
        status_key = "copied_agent_name" if str(copy_kind or "").strip() == "name" else "copied_agent_id"
        self._update_status(self.translator.text(status_key))

    async def _open_agent_steer_input(self, target_agent_id: str) -> None:
        session_id = self._active_session_id()
        if not session_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        agent = self.agent_states.get(str(target_agent_id or "").strip())
        agent_name = agent.name if agent is not None else str(target_agent_id or "").strip()
        self.push_screen(
            SteerInputScreen(
                translator=self.translator,
                agent_name=agent_name,
            ),
            lambda result: self._on_steer_input_dismissed(target_agent_id, result),
        )

    def _on_steer_input_dismissed(self, target_agent_id: str, result: object | None) -> None:
        if result is None:
            self._update_status(self.translator.text("steer_submit_cancelled"))
            return
        content = str(result).strip()
        if not content:
            self._update_status(self.translator.text("steer_input_required"))
            return
        asyncio.create_task(self._submit_steer_content(target_agent_id, content))

    async def _submit_steer_content(self, target_agent_id: str, content: str) -> None:
        session_id = self._active_session_id()
        if not session_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            orchestrator.submit_steer_run(
                session_id=session_id,
                agent_id=str(target_agent_id or "").strip(),
                content=content,
                source="tui",
            )
            target_role = self._agent_role_in_session(
                orchestrator=orchestrator,
                session_id=session_id,
                agent_id=str(target_agent_id or "").strip(),
            )
            run_root_agent = target_role in {"", "root"}
            session_row = orchestrator.storage.load_session(session_id)
            session_status = (
                str(session_row.get("status", "")).strip().lower()
                if isinstance(session_row, dict)
                else ""
            )
            normalized_target_agent_id = str(target_agent_id or "").strip()
            if session_status != "running" and not self._has_running_session():
                selected_model = self._selected_model_for_run(self.selected_model)
                self.selected_model = selected_model
                instruction = self._steer_resume_instruction(str(target_agent_id or "").strip())
                self.current_task = instruction
                self.current_session_id = session_id
                self.configured_resume_session_id = session_id
                self.current_session_status = "resuming"
                self.current_summary = ""
                self._set_controls_running(True)
                self._update_status(self.translator.text("resume_started"))
                self.orchestrator = orchestrator
                self.session_task = asyncio.create_task(
                    self._continue_session(
                        session_id,
                        instruction,
                        selected_model,
                        reactivate_agent_id=normalized_target_agent_id or None,
                        run_root_agent=run_root_agent,
                        remote_password=self.remote_password,
                    )
                )
            self._steer_runs_dirty = True
            self._render_steer_runs_panel()
            self._update_status(self.translator.text("steer_submitted"))
        except Exception as exc:
            self._update_status(
                f"{self.translator.text('steer_submit_failed')}: {exc}"
            )
        finally:
            if created_local:
                del orchestrator

    async def _terminate_agent_from_live_panel(self, target_agent_id: str) -> None:
        session_id = self._active_session_id()
        normalized_target_agent_id = str(target_agent_id or "").strip()
        if not session_id or not normalized_target_agent_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            result = await orchestrator.terminate_agent_subtree(
                session_id=session_id,
                agent_id=normalized_target_agent_id,
                source="tui",
            )
            terminated_count = len(result.get("terminated_agent_ids", []))
            cancelled_tool_runs = len(result.get("cancelled_tool_run_ids", []))
            if terminated_count > 0:
                self._update_status(
                    (
                        f"{self.translator.text('agent_terminate_requested')} "
                        f"({terminated_count} agents, {cancelled_tool_runs} tool runs)"
                    )
                )
            else:
                self._update_status(self.translator.text("agent_terminate_noop"))
            self._tool_runs_dirty = True
            self._queue_agent_panel_refresh()
            self._render_tool_runs_panel()
        except Exception as exc:
            self._update_status(
                f"{self.translator.text('agent_terminate_failed')}: {exc}"
            )
        finally:
            if created_local:
                del orchestrator

    @staticmethod
    def _steer_resume_instruction(agent_id: str) -> str:
        return (
            "A steer message was submitted while this session was inactive. "
            f"Reactivate agent {agent_id} and continue execution so pending steer instructions are consumed."
        )

    async def _cancel_steer_run_from_panel(self, steer_run_id: str) -> None:
        session_id = self._active_session_id()
        normalized_run_id = str(steer_run_id or "").strip()
        if not session_id or not normalized_run_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            result = orchestrator.cancel_steer_run(
                session_id=session_id,
                steer_run_id=normalized_run_id,
            )
            final_status = str(result.get("final_status", "")).strip().lower()
            if final_status == "cancelled":
                self._update_status(self.translator.text("steer_cancelled"))
            elif final_status == "completed":
                self._update_status(self.translator.text("steer_cancel_blocked_completed"))
            else:
                self._update_status(
                    f"{self.translator.text('steer_cancel_failed')}: {final_status or self.translator.text('invalid_value')}"
                )
            self._steer_runs_dirty = True
            self._render_steer_runs_panel()
        except Exception as exc:
            self._update_status(
                f"{self.translator.text('steer_cancel_failed')}: {exc}"
            )
        finally:
            if created_local:
                del orchestrator

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "root_agent_name_input":
            return
        if event.input.id != "model_input":
            return
        self.selected_model = self._selected_model_for_run(event.value)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "task_input":
            if (
                not self._task_input_programmatic_update
                and self._task_input_seeded_default is not None
                and event.text_area.text != self._task_input_seeded_default
            ):
                self._task_input_seeded_default = None
            self._update_task_input_height()
            return
        if event.text_area.id != "config_editor" or self._config_editor_updating:
            return
        self._config_editor_dirty = event.text_area.text != self._config_last_saved_text
        if self._config_editor_dirty:
            if self._config_pending_external_mtime_ns is not None:
                self._set_config_notice("config_external_conflict")
            else:
                self._set_config_notice("config_unsaved_changes")
            self._update_config_controls()
            return
        if self._config_pending_external_mtime_ns is not None:
            self._reload_config_from_disk(force=True, source="external")
            return
        if self._config_notice_key in {
            "config_unsaved_changes",
            "config_external_conflict",
            "config_invalid_toml",
            "config_save_failed",
        }:
            self._set_config_notice("config_sync_clean")
        self._update_config_controls()

    async def on_runtime_update(self, message: RuntimeUpdate) -> None:
        record = message.payload
        try:
            self._consume_runtime_update(record)
            if self._should_write_activity(record):
                activity_log = self._query_optional("#activity_log", RichLog)
                if activity_log is not None:
                    activity_log.write(self._format_event(record))
            if self._should_sync_messages_for_event(record):
                await self._sync_session_messages_incremental()
        except asyncio.CancelledError:
            self._log_diagnostic("runtime_update_cancelled", level="warning")
            return
        except Exception as exc:
            if self.orchestrator is not None:
                self.orchestrator.request_interrupt()
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._log_diagnostic("runtime_update_failed", level="error", error=exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] UI update failure: {exc}")
            self._finalize_session_ui()

    def _should_sync_messages_for_event(self, record: dict[str, Any]) -> bool:
        if self._history_replay_in_progress:
            return False
        if not self._active_session_id():
            return False
        event_type = str(record.get("event_type", ""))
        return event_type in {
            "session_started",
            "session_resumed",
            "session_finalized",
            "session_failed",
            "session_interrupted",
            "agent_prompt",
            "agent_response",
            "tool_call",
            "control_message",
            "child_summaries_received",
            "agent_completed",
        }

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        self._remember_collapsible_toggle(event.collapsible, expanded=True)

    def on_collapsible_collapsed(self, event: Collapsible.Collapsed) -> None:
        self._remember_collapsible_toggle(event.collapsible, expanded=False)

    async def _start_run(self) -> None:
        task = self.query_one("#task_input", TextArea).text.strip()
        if not task:
            self._update_status(self.translator.text("error_task_required"))
            return
        model_input = self.query_one("#model_input", Input)
        selected_model = self._selected_model_for_run(model_input.value)
        root_agent_name_input = self._query_optional("#root_agent_name_input", Input)
        root_agent_name = (
            str(root_agent_name_input.value or "").strip()
            if root_agent_name_input is not None
            else ""
        )
        self.selected_model = selected_model
        if not model_input.value.strip():
            model_input.value = selected_model
        if self._has_running_session():
            active_session_id = (self.current_session_id or "").strip()
            requested_session_id = (self.configured_resume_session_id or "").strip()
            submit_session_id = requested_session_id or active_session_id
            if not submit_session_id:
                self._update_status(self.translator.text("already_running"))
                return
            if active_session_id and submit_session_id != active_session_id:
                self._update_status(self.translator.text("already_running"))
                return
            if self.orchestrator is None:
                self._update_status(self.translator.text("already_running"))
                return
            self.orchestrator.submit_run_in_active_session(
                submit_session_id,
                task,
                model=selected_model,
                root_agent_name=root_agent_name or None,
                source="tui",
            )
            self.current_task = task
            self.current_session_id = submit_session_id
            self.configured_resume_session_id = submit_session_id
            self.current_session_status = "running"
            self._update_status(self.translator.text("started"))
            return
        if not self._can_start_session("run"):
            return
        session_id = (self.configured_resume_session_id or "").strip()
        if session_id:
            self._prepare_session_view(
                task=task,
                session_id=session_id,
                status="starting",
            )
            self.orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            self.app_dir = self.orchestrator.app_dir
            self.orchestrator.subscribe(self._post_runtime_update)
            self._restore_session_history(session_id, import_context=False)
            self.current_session_status = "starting"
            self._update_status(self.translator.text("started"))
            self._set_controls_running(True)
            self._log_diagnostic(
                "run_requested",
                payload={
                    "session_id": session_id,
                    "task": task,
                    "model": selected_model,
                    "root_agent_name": root_agent_name,
                },
            )
            self.session_task = asyncio.create_task(
                self._run_task_in_session(
                    session_id,
                    task,
                    selected_model,
                    root_agent_name,
                    self.remote_password,
                )
            )
            return

        self._prepare_session_view(task=task, session_id=None, status="starting")
        has_remote = self.remote_config is not None
        if not has_remote and self.project_dir is None:
            self._update_status(self.translator.text("error_config_required"))
            return
        self.orchestrator = self._create_orchestrator(
            self.project_dir or Path.cwd(),
            locale=self.locale,
            app_dir=self.app_dir,
        )
        self.app_dir = self.orchestrator.app_dir
        self.orchestrator.subscribe(self._post_runtime_update)
        self._update_status(self.translator.text("started"))
        self._set_controls_running(True)
        self._log_diagnostic(
            "run_requested",
            payload={
                "task": task,
                "model": selected_model,
                "root_agent_name": root_agent_name,
                "project_dir": str(self.project_dir) if self.project_dir is not None else None,
                "remote_target": self.remote_config.ssh_target if self.remote_config else None,
            },
        )
        self.session_task = asyncio.create_task(
            self._run_task(
                task,
                selected_model,
                root_agent_name,
                self.session_mode,
                self.remote_config,
                self.remote_password,
            )
        )

    def _active_session_id(self) -> str | None:
        return self.current_session_id or self.configured_resume_session_id

    def _workspace_mode(self) -> WorkspaceMode:
        return normalize_workspace_mode(self.session_mode)

    def _workspace_mode_label(self, mode: WorkspaceMode | str | None = None) -> str:
        resolved = normalize_workspace_mode(mode or self.session_mode)
        return self.translator.text(f"workspace_mode_{resolved.value}")

    def _is_direct_workspace_mode(self) -> bool:
        return self._workspace_mode() == WorkspaceMode.DIRECT

    def _config_file_path(self) -> Path:
        return self._resolved_app_dir() / "opencompany.toml"

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
        config = getattr(orchestrator, "config", None)
        tool_executor = getattr(orchestrator, "tool_executor", None)
        sandbox = getattr(config, "sandbox", None) if config is not None else None
        # Some tests monkeypatch a minimal orchestrator stub without sandbox internals.
        # In that case, keep the selected backend value but skip backend wiring.
        if sandbox is not None and tool_executor is not None:
            sandbox.backend = backend_name
            backend_cls = resolve_sandbox_backend_cls(sandbox)
            if hasattr(tool_executor, "sandbox_backend_cls"):
                tool_executor.sandbox_backend_cls = backend_cls
            if hasattr(tool_executor, "_shell_backend_instance"):
                tool_executor._shell_backend_instance = None  # type: ignore[attr-defined]
        self.sandbox_backend = backend_name

    def _sandbox_backend_label(self, backend: str | None = None) -> str:
        normalized = self._normalize_sandbox_backend(backend or self.sandbox_backend)
        key = f"sandbox_backend_{normalized}"
        translated = self.translator.text(key)
        return normalized if translated == key else translated

    def _selected_model_for_run(self, value: str | None) -> str:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
        return self._default_model_from_config()

    def _ensure_model_input_value(self) -> None:
        model_input = self._query_optional("#model_input", Input)
        if model_input is None:
            return
        normalized = str(model_input.value or "").strip()
        if normalized:
            self.selected_model = normalized
            return
        self.selected_model = self._selected_model_for_run(self.selected_model)
        model_input.value = self.selected_model

    def _refresh_locale_from_config(self) -> None:
        if self._locale_override in {"en", "zh"}:
            return
        desired = self._resolve_configured_locale()
        if desired != self.locale:
            self._set_locale(desired, remember_override=False)

    def _poll_external_config_changes(self) -> None:
        path = self._config_file_path()
        try:
            mtime_ns = path.stat().st_mtime_ns if path.exists() else None
        except OSError as exc:
            self._set_config_notice("config_load_failed", detail=str(exc))
            self._update_config_controls()
            return
        if mtime_ns == self._config_last_mtime_ns:
            return
        if self._config_editor_dirty:
            self._config_pending_external_mtime_ns = mtime_ns
            self._set_config_notice("config_external_conflict")
            self._update_config_controls()
            return
        self._reload_config_from_disk(force=True, source="external")

    def _reload_config_from_disk(self, *, force: bool, source: str) -> None:
        if self._config_editor_dirty and not force:
            self._set_config_notice("config_unsaved_changes")
            self._update_config_controls()
            return
        path = self._config_file_path()
        self._render_config_path()
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            mtime_ns = path.stat().st_mtime_ns if path.exists() else None
        except OSError as exc:
            self._set_config_notice("config_load_failed", detail=str(exc))
            self._update_config_controls()
            return

        editor = self._query_optional("#config_editor", TextArea)
        if editor is not None:
            self._config_editor_updating = True
            editor.load_text(text)
            self.call_later(self._finish_config_editor_update)
        self._config_last_saved_text = text
        self._config_last_mtime_ns = mtime_ns
        self._config_pending_external_mtime_ns = None
        self._config_editor_dirty = False
        if source == "manual":
            self._set_config_notice("config_reloaded")
        elif source == "external":
            self._set_config_notice("config_reloaded_external")
        else:
            self._set_config_notice("config_sync_clean")
        self._update_config_controls()
        self._refresh_locale_from_config()
        self.default_keep_pinned_messages = self._default_keep_pinned_messages_from_config()

    def _save_config_from_editor(self) -> None:
        editor = self._query_optional("#config_editor", TextArea)
        text = editor.text if editor is not None else self._config_last_saved_text
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self._set_config_notice("config_invalid_toml", detail=str(exc))
            self._update_config_controls()
            return

        path = self._config_file_path()
        self._render_config_path()
        try:
            path.write_text(text, encoding="utf-8")
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            self._set_config_notice("config_save_failed", detail=str(exc))
            self._update_config_controls()
            return

        self._config_last_saved_text = text
        self._config_last_mtime_ns = mtime_ns
        self._config_pending_external_mtime_ns = None
        self._config_editor_dirty = False
        self._set_config_notice("config_saved")
        self._update_config_controls()
        self._refresh_locale_from_config()

    def _set_config_notice(self, key: str, *, detail: str = "") -> None:
        self._config_notice_key = key
        self._config_notice_detail = detail.strip()
        self._render_config_notice()

    def _render_config_notice(self) -> None:
        widget = self._query_optional("#config_sync_status", Static)
        if widget is None:
            return
        message = self.translator.text(self._config_notice_key)
        if self._config_notice_detail:
            message = f"{message}: {self._config_notice_detail}"
        widget.update(message)

    def _render_config_path(self) -> None:
        widget = self._query_optional("#config_path", Static)
        if widget is None:
            return
        widget.update(f"{self.translator.text('config_file_path')}: {self._config_file_path()}")

    def _update_config_controls(self) -> None:
        save_button = self._query_optional("#config_save_button", Button)
        reload_button = self._query_optional("#config_reload_button", Button)
        if save_button is not None:
            save_button.disabled = not self._config_editor_dirty
            save_button.variant = "primary" if self._config_editor_dirty else "default"
        if reload_button is not None:
            reload_button.disabled = False

    def _finish_config_editor_update(self) -> None:
        self._config_editor_updating = False

    def _refresh_project_sync_state(self) -> None:
        self._invalidate_diff_preview_cache()
        session_id = self._active_session_id()
        if not session_id:
            self.project_sync_state = None
            return
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            state = orchestrator.project_sync_status(session_id)
            self.project_sync_state = state
        except Exception as exc:
            self.project_sync_state = {"status": "error", "last_error": str(exc)}
            self._log_diagnostic(
                "project_sync_status_failed",
                level="warning",
                payload={"session_id": session_id},
                error=exc,
            )
        finally:
            if created_local:
                del orchestrator

    def _project_sync_status(self) -> str:
        if not self.project_sync_state:
            return "none"
        return str(self.project_sync_state.get("status", "none"))

    def _can_apply_project_sync(self) -> bool:
        return self._project_sync_status() in {"pending", "reverted"}

    def _can_undo_project_sync(self) -> bool:
        return self._project_sync_status() == "applied"

    def _project_sync_action_runner(self, action: str, session_id: str):
        method_name = "apply_project_sync" if action == "apply" else "undo_project_sync"
        orchestrator = self.orchestrator
        if orchestrator is not None and not isinstance(orchestrator, Orchestrator):
            return lambda: getattr(orchestrator, method_name)(session_id)

        project_dir = self.project_dir or Path.cwd()
        app_dir = self.app_dir or (getattr(orchestrator, "app_dir", None) if orchestrator is not None else None)
        locale = self.locale

        # Apply / undo runs in a worker thread, so it must not reuse a main-thread sqlite connection.
        def run() -> dict[str, Any]:
            worker_orchestrator = self._create_orchestrator(
                project_dir,
                locale=locale,
                app_dir=app_dir,
            )
            return getattr(worker_orchestrator, method_name)(session_id)

        return run

    def _restore_session_history(self, session_id: str, *, import_context: bool = True) -> None:
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            history_session_id = session_id
            agent_snapshot: list[dict[str, Any]] = []
            if import_context and hasattr(orchestrator, "load_session_context"):
                loaded_session = orchestrator.load_session_context(session_id)
                loaded_remote_config = load_remote_session_config(
                    self._sessions_root_dir() / loaded_session.id
                )
                self.remote_config = loaded_remote_config
                self.remote_password = ""
                self.project_dir = (
                    None
                    if loaded_remote_config is not None
                    else loaded_session.project_dir.resolve()
                )
                self.configured_resume_session_id = loaded_session.id
                self.current_session_id = loaded_session.id
                self.session_mode = normalize_workspace_mode(loaded_session.workspace_mode)
                self.session_mode_locked = True
                self.current_task = loaded_session.task
                self.current_session_status = loaded_session.status.value
                self.current_summary = loaded_session.final_summary or ""
                history_session_id = loaded_session.id
            records = orchestrator.load_session_events(history_session_id)
            if hasattr(orchestrator, "load_session_agents"):
                agent_snapshot = orchestrator.load_session_agents(history_session_id)
        except Exception as exc:
            self._log_diagnostic(
                "session_history_restore_failed",
                level="warning",
                payload={"session_id": session_id},
                error=exc,
            )
            return
        finally:
            if created_local:
                del orchestrator
        self._replay_session_history(records)
        self._reload_session_messages(history_session_id)
        self._apply_agent_snapshot(agent_snapshot)
        self._refresh_project_sync_state()
        self._set_controls_running(self._has_running_session())
        self._render_all()

    def _replay_session_history(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self._history_replay_in_progress = True
        activity_log = self._query_optional("#activity_log", RichLog)
        try:
            for record in records:
                self._consume_runtime_update(record, render=False)
                if activity_log is not None and self._should_write_activity(record):
                    activity_log.write(self._format_event(record))
        finally:
            self._history_replay_in_progress = False
        self._render_all()

    def _reload_session_messages(self, session_id: str) -> None:
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        if not hasattr(orchestrator, "list_session_messages"):
            if created_local:
                del orchestrator
            return
        try:
            message_records, next_cursor = self._collect_session_messages(orchestrator, session_id)
        except Exception as exc:
            self._log_diagnostic(
                "session_messages_restore_failed",
                level="warning",
                payload={"session_id": session_id},
                error=exc,
            )
            return
        finally:
            if created_local:
                del orchestrator
        self._replace_message_stream_entries(message_records, preserve_non_message=False)
        self._message_cursor = next_cursor
        self._render_all()

    def _collect_session_messages(
        self,
        orchestrator: Orchestrator,
        session_id: str,
        *,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        records: list[dict[str, Any]] = []
        next_cursor = cursor
        for _ in range(200):
            page = orchestrator.list_session_messages(
                session_id,
                cursor=next_cursor,
                limit=500,
            )
            chunk = page.get("messages", [])
            if isinstance(chunk, list):
                for record in chunk:
                    if isinstance(record, dict):
                        records.append(record)
            candidate_cursor = page.get("next_cursor")
            has_more = bool(page.get("has_more", False))
            if isinstance(candidate_cursor, str) and candidate_cursor.strip():
                if candidate_cursor == next_cursor and has_more:
                    break
                next_cursor = candidate_cursor
            if not has_more:
                break
        return records, next_cursor

    def _apply_agent_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        if not snapshot:
            return
        for row in snapshot:
            if not isinstance(row, dict):
                continue
            agent_id = str(row.get("id", "")).strip()
            if not agent_id:
                continue
            raw_parent_agent_id = row.get("parent_agent_id")
            parent_agent_id = (
                None
                if raw_parent_agent_id is None
                else (str(raw_parent_agent_id).strip() or None)
            )
            details = {
                "agent_name": row.get("name"),
                "agent_role": row.get("role"),
                "instruction": row.get("instruction"),
                "step_count": row.get("step_count"),
                "agent_status": row.get("status"),
                "agent_model": row.get("model"),
                "current_context_tokens": row.get("current_context_tokens"),
                "context_limit_tokens": row.get("context_limit_tokens"),
                "usage_ratio": row.get("usage_ratio"),
                "last_usage_input_tokens": row.get("last_usage_input_tokens"),
                "last_usage_output_tokens": row.get("last_usage_output_tokens"),
                "last_usage_cache_read_tokens": row.get("last_usage_cache_read_tokens"),
                "last_usage_cache_write_tokens": row.get("last_usage_cache_write_tokens"),
                "last_usage_total_tokens": row.get("last_usage_total_tokens"),
                "compression_count": row.get("compression_count"),
                "keep_pinned_messages": row.get("keep_pinned_messages"),
                "summary_version": row.get("summary_version"),
                "context_latest_summary": row.get("context_latest_summary"),
                "summarized_until_message_index": row.get("summarized_until_message_index"),
                "last_compacted_step_range": row.get("last_compacted_step_range"),
            }
            state = self._ensure_agent_state(
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
                details=details,
            )
            state.parent_agent_id = parent_agent_id
            status = str(row.get("status", "")).strip()
            if status:
                state.status = status
            try:
                step_count = int(row.get("step_count", state.step_count) or 0)
            except (TypeError, ValueError):
                step_count = state.step_count
            if step_count >= 0:
                state.step_count = max(state.step_count, step_count)
            if row.get("summary") is not None:
                state.summary = str(row.get("summary", ""))
        self._render_all()

    async def _sync_session_messages_incremental(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            return
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        if not hasattr(orchestrator, "list_session_messages"):
            if created_local:
                del orchestrator
            return
        try:
            records, next_cursor = self._collect_session_messages(
                orchestrator,
                session_id,
                cursor=self._message_cursor,
            )
        except Exception as exc:
            self._log_diagnostic(
                "session_messages_incremental_failed",
                level="warning",
                payload={"session_id": session_id},
                error=exc,
            )
            return
        finally:
            if created_local:
                del orchestrator
        if records:
            self._apply_message_records(records)
        self._message_cursor = next_cursor

    async def _apply_project_sync(self) -> None:
        if self._has_running_session():
            self._update_status(self.translator.text("already_running"))
            return
        session_id = self._active_session_id()
        if not session_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        if not self._can_apply_project_sync():
            self._update_status(self.translator.text("sync_apply_unavailable"))
            return
        orchestrator = self.orchestrator
        if orchestrator is not None and getattr(orchestrator, "app_dir", None) is not None:
            self.app_dir = orchestrator.app_dir
        self.project_sync_action_in_progress = True
        self._set_controls_running(False)
        self._update_status(self.translator.text("sync_apply_started"))
        self._log_diagnostic("project_sync_apply_requested", payload={"session_id": session_id})
        try:
            result = await asyncio.to_thread(self._project_sync_action_runner("apply", session_id))
            self.project_sync_state = {
                "status": str(result.get("status", "applied")),
                "added": int(result.get("added", 0)),
                "modified": int(result.get("modified", 0)),
                "deleted": int(result.get("deleted", 0)),
                "backup_dir": result.get("backup_dir"),
            }
            self.last_project_sync_operation = {
                "operation": "apply",
                "project_dir": str(result.get("project_dir", self.project_dir or "")),
                "added": int(result.get("added", 0)),
                "modified": int(result.get("modified", 0)),
                "deleted": int(result.get("deleted", 0)),
            }
            summary = (
                f"{self.translator.text('sync_apply_done')} "
                f"(+{int(result.get('added', 0))}/~{int(result.get('modified', 0))}/-{int(result.get('deleted', 0))})"
            )
            self._update_status(summary)
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(summary)
        except Exception as exc:
            self._update_status(f"{self.translator.text('sync_failed')}: {exc}")
            self._log_diagnostic(
                "project_sync_apply_failed",
                level="error",
                payload={"session_id": session_id},
                error=exc,
            )
        finally:
            self.project_sync_action_in_progress = False
            self._refresh_project_sync_state()
            self._set_controls_running(False)
            self._render_all()

    async def _undo_project_sync(self) -> None:
        if self._has_running_session():
            self._update_status(self.translator.text("already_running"))
            return
        session_id = self._active_session_id()
        if not session_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        if not self._can_undo_project_sync():
            self._update_status(self.translator.text("sync_undo_unavailable"))
            return
        orchestrator = self.orchestrator
        if orchestrator is not None and getattr(orchestrator, "app_dir", None) is not None:
            self.app_dir = orchestrator.app_dir
        self.project_sync_action_in_progress = True
        self._set_controls_running(False)
        self._update_status(self.translator.text("sync_undo_started"))
        self._log_diagnostic("project_sync_undo_requested", payload={"session_id": session_id})
        try:
            result = await asyncio.to_thread(self._project_sync_action_runner("undo", session_id))
            self.project_sync_state = {
                "status": str(result.get("status", "reverted")),
                "removed": int(result.get("removed", 0)),
                "restored": int(result.get("restored", 0)),
                "missing_backups": list(result.get("missing_backups", [])),
            }
            self.last_project_sync_operation = {
                "operation": "undo",
                "project_dir": str(result.get("project_dir", self.project_dir or "")),
                "removed": int(result.get("removed", 0)),
                "restored": int(result.get("restored", 0)),
                "missing_backups": list(result.get("missing_backups", [])),
            }
            summary = (
                f"{self.translator.text('sync_undo_done')} "
                f"(removed={int(result.get('removed', 0))}, restored={int(result.get('restored', 0))})"
            )
            missing = list(result.get("missing_backups", []))
            if missing:
                summary += f" | missing backups: {', '.join(str(item) for item in missing)}"
            self._update_status(summary)
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(summary)
        except Exception as exc:
            self._update_status(f"{self.translator.text('sync_failed')}: {exc}")
            self._log_diagnostic(
                "project_sync_undo_failed",
                level="error",
                payload={"session_id": session_id},
                error=exc,
            )
        finally:
            self.project_sync_action_in_progress = False
            self._refresh_project_sync_state()
            self._set_controls_running(False)
            self._render_all()

    async def _run_task(
        self,
        task: str,
        model: str,
        root_agent_name: str | None = None,
        session_mode: WorkspaceMode | str | None = None,
        remote_config: RemoteSessionConfig | None = None,
        remote_password: str | None = None,
    ) -> None:
        assert self.orchestrator is not None
        try:
            run_kwargs: dict[str, Any] = {
                "model": model,
                "root_agent_name": root_agent_name or None,
            }
            if normalize_workspace_mode(session_mode or WorkspaceMode.DIRECT.value) != WorkspaceMode.DIRECT:
                run_kwargs["workspace_mode"] = session_mode
            if remote_config is not None:
                run_kwargs["remote_config"] = remote_config
                if str(remote_password or "").strip():
                    run_kwargs["remote_password"] = remote_password
            session = await self.orchestrator.run_task(task, **run_kwargs)
            self.project_dir = session.project_dir.resolve()
            if hasattr(self.orchestrator, "_session_remote_config"):
                resolved_remote = self.orchestrator._session_remote_config(session.id)  # type: ignore[attr-defined]
                self.remote_config = resolved_remote
            if self.remote_config is None:
                self.remote_password = ""
            self.configured_resume_session_id = session.id
            self.current_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self.current_session_status = session.status.value
            self.current_summary = session.final_summary or self.current_summary
            self._refresh_project_sync_state()
            self._update_status(self.current_summary or self.translator.text("session_completed"))
        except asyncio.CancelledError:
            cancelling = asyncio.current_task().cancelling() if asyncio.current_task() else 0
            interrupt_requested = bool(
                self.orchestrator and getattr(self.orchestrator, "interrupt_requested", False)
            )
            self._log_diagnostic(
                "run_cancelled",
                level="warning",
                payload={
                    "interrupt_requested": interrupt_requested,
                    "cancelling": cancelling,
                },
            )
            self.current_session_status = "interrupted"
            self._update_status(self.translator.text("session_interrupted"))
        except Exception as exc:
            self._log_diagnostic("run_failed", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] {self.translator.text('session_failed')}: {exc}")
        except BaseException as exc:
            self._log_diagnostic("run_fatal_error", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] Fatal runtime error: {exc}")
        finally:
            self.session_task = None
            self._log_diagnostic(
                "run_finished",
                payload={"status": self.current_session_status, "session_id": self.current_session_id},
            )
            self._finalize_session_ui()

    async def _run_task_in_session(
        self,
        session_id: str,
        task: str,
        model: str,
        root_agent_name: str | None = None,
        remote_password: str | None = None,
    ) -> None:
        assert self.orchestrator is not None
        try:
            run_kwargs: dict[str, Any] = {
                "model": model,
                "root_agent_name": root_agent_name or None,
            }
            if str(remote_password or "").strip():
                run_kwargs["remote_password"] = remote_password
            session = await self.orchestrator.run_task_in_session(
                session_id,
                task,
                **run_kwargs,
            )
            self.project_dir = session.project_dir.resolve()
            if hasattr(self.orchestrator, "_session_remote_config"):
                resolved_remote = self.orchestrator._session_remote_config(session.id)  # type: ignore[attr-defined]
                self.remote_config = resolved_remote
            if self.remote_config is None:
                self.remote_password = ""
            self.configured_resume_session_id = session.id
            self.current_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self.current_session_status = session.status.value
            self.current_summary = session.final_summary or self.current_summary
            self._refresh_project_sync_state()
            self._update_status(self.current_summary or self.translator.text("session_completed"))
        except asyncio.CancelledError:
            cancelling = asyncio.current_task().cancelling() if asyncio.current_task() else 0
            interrupt_requested = bool(
                self.orchestrator and getattr(self.orchestrator, "interrupt_requested", False)
            )
            self._log_diagnostic(
                "run_cancelled",
                level="warning",
                payload={
                    "interrupt_requested": interrupt_requested,
                    "cancelling": cancelling,
                },
            )
            self.current_session_status = "interrupted"
            self._update_status(self.translator.text("session_interrupted"))
        except Exception as exc:
            self._log_diagnostic("run_failed", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] {self.translator.text('session_failed')}: {exc}")
        except BaseException as exc:
            self._log_diagnostic("run_fatal_error", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] Fatal runtime error: {exc}")
        finally:
            self.session_task = None
            self._log_diagnostic(
                "run_finished",
                payload={"status": self.current_session_status, "session_id": self.current_session_id},
            )
            self._finalize_session_ui()

    async def _continue_session(
        self,
        session_id: str,
        instruction: str,
        model: str,
        reactivate_agent_id: str | None = None,
        run_root_agent: bool = True,
        remote_password: str | None = None,
    ) -> None:
        assert self.orchestrator is not None
        try:
            resume_kwargs: dict[str, Any] = {
                "model": model,
                "reactivate_agent_id": reactivate_agent_id,
                "run_root_agent": run_root_agent,
            }
            if str(remote_password or "").strip():
                resume_kwargs["remote_password"] = remote_password
            session = await self.orchestrator.resume(
                session_id,
                instruction,
                **resume_kwargs,
            )
            self.project_dir = session.project_dir.resolve()
            if hasattr(self.orchestrator, "_session_remote_config"):
                resolved_remote = self.orchestrator._session_remote_config(session.id)  # type: ignore[attr-defined]
                self.remote_config = resolved_remote
            if self.remote_config is None:
                self.remote_password = ""
            self.configured_resume_session_id = session.id
            self.current_session_id = session.id
            self.session_mode = normalize_workspace_mode(session.workspace_mode)
            self.session_mode_locked = True
            self.current_session_status = session.status.value
            self.current_summary = session.final_summary or self.current_summary
            self._refresh_project_sync_state()
            self._update_status(self.current_summary or self.translator.text("session_resumed_done"))
        except asyncio.CancelledError:
            cancelling = asyncio.current_task().cancelling() if asyncio.current_task() else 0
            interrupt_requested = bool(
                self.orchestrator and getattr(self.orchestrator, "interrupt_requested", False)
            )
            self._log_diagnostic(
                "continue_cancelled",
                level="warning",
                payload={
                    "interrupt_requested": interrupt_requested,
                    "cancelling": cancelling,
                },
            )
            self.current_session_status = "interrupted"
            self._update_status(self.translator.text("session_interrupted"))
        except Exception as exc:
            self._log_diagnostic("continue_failed", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] {self.translator.text('session_failed')}: {exc}")
        except BaseException as exc:
            self._log_diagnostic("continue_fatal_error", level="error", error=exc)
            self.current_session_status = "failed"
            self.current_summary = str(exc)
            self._update_status(f"{self.translator.text('session_failed')}: {exc}")
            activity_log = self._query_optional("#activity_log", RichLog)
            if activity_log is not None:
                activity_log.write(f"[runtime] Fatal runtime error: {exc}")
        finally:
            self.session_task = None
            self._log_diagnostic(
                "continue_finished",
                payload={"status": self.current_session_status, "session_id": self.current_session_id},
            )
            self._finalize_session_ui()

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
        for row in orchestrator.storage.load_agents(session_id):
            if str(row.get("id", "")).strip() != normalized_agent_id:
                continue
            return str(row.get("role", "")).strip().lower()
        return ""

    async def _cancel_active_session_task(self) -> None:
        task = self.session_task
        if task is None or task.done():
            return
        self._log_diagnostic("active_session_cancel_requested", level="warning")
        if self.orchestrator is not None:
            self.orchestrator.request_interrupt()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._log_diagnostic("active_session_cancel_finished")

    def _post_runtime_update(self, payload: dict[str, Any]) -> None:
        try:
            self.post_message(RuntimeUpdate(payload))
        except asyncio.CancelledError:
            # The app can be shutting down while runtime events are still being delivered.
            return
        except Exception:
            # During shutdown the message pump may already be gone.
            return

    def _log_diagnostic(
        self,
        event_type: str,
        *,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
        message: str = "",
    ) -> None:
        app_dir = self._resolved_app_dir()
        if self._diagnostics is None:
            self._diagnostics = DiagnosticLogger(diagnostics_path_for_app(app_dir))
        self._diagnostics.log(
            component="tui",
            event_type=event_type,
            level=level,
            session_id=self.current_session_id or self.configured_resume_session_id,
            message=message,
            payload={
                "session_status": self.current_session_status,
                "running": self._has_running_session(),
                "project_dir": str(self.project_dir) if self.project_dir is not None else None,
                **(payload or {}),
            },
            error=error,
        )

    def _resolved_app_dir(self) -> Path:
        if self.app_dir is not None:
            return self.app_dir.resolve()
        if self.orchestrator is not None:
            orchestrator_app_dir = getattr(self.orchestrator, "app_dir", None)
            if orchestrator_app_dir is not None:
                return Path(orchestrator_app_dir).resolve()
        try:
            return default_app_dir()
        except RuntimeError:
            return Path.cwd().resolve()

    def _finalize_session_ui(self) -> None:
        # The app may be unmounting while background tasks are being cancelled.
        if not self.is_attached:
            return
        try:
            self._set_controls_running(False)
            self._render_all()
        except NoMatches:
            return

    def _has_running_session(self) -> bool:
        return bool(self.session_task and not self.session_task.done())

    def _can_start_session(self, action: str) -> bool:
        if action != "run" and self.session_task and not self.session_task.done():
            self._update_status(self.translator.text("already_running"))
            return False
        if action == "run" and not self._launch_config().can_run():
            if self.project_dir is not None and not self.project_dir.is_dir():
                self._update_status(self.translator.text("error_project_invalid"))
            else:
                self._update_status(self.translator.text("error_config_required"))
            return False
        return True

    def _prepare_session_view(self, *, task: str, session_id: str | None, status: str) -> None:
        self.agent_states = {}
        self.current_task = task
        self.current_session_id = session_id
        self.current_session_status = status
        self.current_focus_agent_id = None
        self.current_summary = ""
        self.project_sync_state = None
        self.stream_agent_order = []
        self.overview_collapsed_agent_ids = set()
        self.overview_instruction_collapsed_agent_ids = set()
        self.overview_summary_expanded_agent_ids = set()
        self.live_collapsed_agent_ids = set()
        self.live_step_collapsed_overrides = {}
        self.last_project_sync_operation = None
        self._diff_preview_cache_key = None
        self._diff_preview_cache_data = None
        self._diff_render_cache_key = None
        self._overview_structure_signature = None
        self._live_structure_signature = None
        self._overview_render_fingerprints = {}
        self._live_agent_render_fingerprints = {}
        self._live_step_render_fingerprints = {}
        self._live_step_entry_render_fingerprints = {}
        self._status_panel_cache_text = ""
        self.tool_runs_snapshot = None
        self.tool_runs_metrics_snapshot = None
        self.tool_runs_status_message = ""
        self.tool_runs_filter = "all"
        self.tool_runs_group_by = "agent"
        self.tool_runs_selected_run_id = None
        self._tool_runs_dirty = True
        self._tool_runs_cache_key = None
        self._tool_run_call_id_to_run_id = {}
        self._tool_run_timeline_by_run_id = {}
        self._tool_runs_detail_open_run_id = None
        self._tool_runs_detail_run_snapshot = None
        self.steer_runs_snapshot = None
        self.steer_runs_metrics_snapshot = None
        self.steer_runs_status_message = ""
        self.steer_runs_filter = "all"
        self.steer_runs_group_by = "agent"
        self._steer_runs_dirty = True
        self._steer_runs_cache_key = None
        self._message_cursor = None
        self._history_replay_in_progress = False
        if isinstance(self.screen, ToolRunDetailScreen):
            self.screen.dismiss(None)
        self._clear_activity_log()
        self._render_all()

    def _set_locale(self, desired: str, *, remember_override: bool = True) -> None:
        if desired not in {"en", "zh"}:
            desired = "en"
        if remember_override:
            self._locale_override = desired
        previous_ready = self.translator.text("ready")
        previous_required = self.translator.text("configuration_required")
        self.locale = desired
        self.translator = Translator(desired)
        if self.status_message in {previous_ready, previous_required}:
            fallback = (
                "ready"
                if self._launch_config().can_run()
                else "configuration_required"
            )
            self.status_message = self.translator.text(fallback)
        self._invalidate_diff_preview_cache()
        self._overview_render_fingerprints = {}
        self._live_agent_render_fingerprints = {}
        self._live_step_render_fingerprints = {}
        self._live_step_entry_render_fingerprints = {}
        self._status_panel_cache_text = ""
        self._static_text_dirty = True
        self._sync_task_input_default(locale_switched=True)
        self._render_all()

    def _update_status(self, text: str) -> None:
        self.status_message = text
        self._render_status_panel()

    def _consume_runtime_update(self, record: dict[str, Any], *, render: bool = True) -> None:
        details = record.get("payload", {})
        event_type = str(record.get("event_type", ""))
        session_id = record.get("session_id")
        had_active_session_id = bool(self._active_session_id())
        if session_id:
            self.current_session_id = str(session_id)
            self.configured_resume_session_id = str(session_id)
            if not had_active_session_id and self._has_running_session():
                self._set_controls_running(True)
        if details.get("task"):
            self.current_task = str(details["task"])
        self._register_tool_run_timeline_event(record)

        if event_type == "session_started":
            self.current_session_status = str(details.get("session_status", "running"))
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True
        elif event_type == "session_resumed":
            self.current_session_status = "running"
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True
        elif event_type == "session_context_imported":
            self.current_session_status = str(
                details.get("session_status", self.current_session_status)
            )
            self._update_status(self.translator.text("configuration_saved"))
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True
        elif event_type == "project_sync_staged":
            self._refresh_project_sync_state()
        elif event_type == "session_finalized":
            self.current_session_status = str(details.get("session_status", "completed"))
            self.current_summary = str(details.get("user_summary", ""))
            self._refresh_project_sync_state()
            self._update_status(self.current_summary or self.translator.text("session_completed"))
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True
        elif event_type == "project_sync_applied":
            self._refresh_project_sync_state()
            self._update_status(self.translator.text("sync_apply_done"))
        elif event_type == "project_sync_reverted":
            self._refresh_project_sync_state()
            self._update_status(self.translator.text("sync_undo_done"))
        elif event_type == "session_interrupted":
            self.current_session_status = str(details.get("session_status", "interrupted"))
            self._update_status(self.translator.text("session_interrupted"))
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True
        elif event_type == "session_failed":
            self.current_session_status = str(details.get("session_status", "failed"))
            self.current_summary = str(details.get("error", ""))
            self._update_status(self.current_summary or self.translator.text("session_failed"))
            self._tool_runs_dirty = True
            self._steer_runs_dirty = True

        agent_id = record.get("agent_id")
        if agent_id:
            state = self._ensure_agent_state(
                agent_id=str(agent_id),
                parent_agent_id=record.get("parent_agent_id"),
                details=details,
            )
            self.current_focus_agent_id = state.id
            if event_type not in LIVE_AGENT_META_SKIP_EVENTS:
                state.last_event = event_type
                state.last_phase = str(record.get("phase", "runtime"))
            state.last_timestamp = str(record.get("timestamp", ""))
            if "step_count" in details:
                state.step_count = int(details["step_count"])
            if details.get("agent_status"):
                state.status = str(details["agent_status"])
            else:
                state.status = self._event_status(event_type, state.status, details=details)
            self._apply_context_metrics(state, details, event_type=event_type)
            if event_type not in LIVE_AGENT_META_SKIP_EVENTS:
                state.last_detail = self._event_detail(record, state)
            self._update_stream_for_event(state, record)
            if event_type == "agent_completed":
                state.summary = str(details.get("summary", ""))
            elif event_type == "session_finalized":
                state.summary = str(details.get("user_summary", state.summary))
            elif event_type == "session_failed":
                state.summary = str(details.get("error", state.summary))

        if event_type in {"tool_run_submitted", "tool_run_updated"}:
            self._tool_runs_dirty = True
        if event_type in {"steer_run_submitted", "steer_run_updated"}:
            self._steer_runs_dirty = True

        if render:
            self._render_all()

    def _ensure_agent_state(
        self,
        *,
        agent_id: str,
        parent_agent_id: str | None,
        details: dict[str, Any],
    ) -> AgentRuntimeView:
        state = self.agent_states.get(agent_id)
        if not state:
            state = AgentRuntimeView(
                id=agent_id,
                name=str(
                    details.get("agent_name")
                    or details.get("root_agent_name")
                    or details.get("name")
                    or agent_id
                ),
                keep_pinned_messages=max(0, int(self.default_keep_pinned_messages)),
            )
            self.agent_states[agent_id] = state
            self.stream_agent_order.append(agent_id)
        if parent_agent_id is not None:
            state.parent_agent_id = str(parent_agent_id)
        state.name = str(
            details.get("agent_name")
            or details.get("root_agent_name")
            or details.get("name")
            or state.name
        )
        state.instruction = str(details.get("instruction") or details.get("task") or state.instruction)
        state.role = str(details.get("agent_role") or details.get("root_agent_role") or state.role)
        model = str(details.get("agent_model") or details.get("model") or "").strip()
        if model:
            state.model = model
        context_tokens = self._coerce_non_negative_int(details.get("current_context_tokens"))
        if context_tokens is not None:
            state.current_context_tokens = context_tokens
        context_limit = self._coerce_non_negative_int(details.get("context_limit_tokens"))
        if context_limit is not None:
            state.context_limit_tokens = context_limit
        try:
            usage_ratio = float(details.get("usage_ratio"))
        except (TypeError, ValueError):
            usage_ratio = None
        if usage_ratio is not None and usage_ratio >= 0:
            state.usage_ratio = usage_ratio
        compression_count = self._coerce_non_negative_int(details.get("compression_count"))
        if compression_count is not None:
            state.compression_count = compression_count
        self._apply_context_summary_metrics(state, details)
        self._apply_usage_metrics(state, details)
        compacted_step_range = self._format_step_range_text(details.get("last_compacted_step_range"))
        if compacted_step_range:
            state.last_compacted_step_range = compacted_step_range
        normalized_compacted = self._normalize_step_range(details.get("last_compacted_step_range"))
        if normalized_compacted is not None and normalized_compacted not in state.compacted_step_ranges:
            state.compacted_step_ranges.append(normalized_compacted)
            state.compacted_step_ranges.sort()
        return state

    def _apply_usage_metrics(
        self,
        state: AgentRuntimeView,
        details: dict[str, Any],
    ) -> None:
        usage_fields = (
            ("last_usage_input_tokens", "last_usage_input_tokens"),
            ("last_usage_output_tokens", "last_usage_output_tokens"),
            ("last_usage_cache_read_tokens", "last_usage_cache_read_tokens"),
            ("last_usage_cache_write_tokens", "last_usage_cache_write_tokens"),
            ("last_usage_total_tokens", "last_usage_total_tokens"),
        )
        for details_key, state_key in usage_fields:
            if details_key not in details:
                continue
            normalized = self._coerce_non_negative_int(details.get(details_key))
            setattr(state, state_key, normalized)

    def _apply_context_summary_metrics(
        self,
        state: AgentRuntimeView,
        details: dict[str, Any],
    ) -> None:
        if "keep_pinned_messages" in details:
            keep_pinned_messages = self._coerce_non_negative_int(details.get("keep_pinned_messages"))
            if keep_pinned_messages is not None:
                state.keep_pinned_messages = keep_pinned_messages
        if "summary_version" in details:
            summary_version = self._coerce_non_negative_int(details.get("summary_version"))
            state.summary_version = summary_version if summary_version is not None else 0
        if "context_latest_summary" in details:
            raw_summary = details.get("context_latest_summary")
            state.context_latest_summary = (
                ""
                if raw_summary is None
                else str(raw_summary)
            )
        if "summarized_until_message_index" in details:
            raw_index = details.get("summarized_until_message_index")
            if raw_index is None:
                state.summarized_until_message_index = None
            else:
                try:
                    normalized = int(raw_index)
                except (TypeError, ValueError):
                    state.summarized_until_message_index = None
                else:
                    state.summarized_until_message_index = max(-1, normalized)

    def _event_status(
        self,
        event_type: str,
        current_status: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> str:
        payload = details if isinstance(details, dict) else {}
        if event_type == "session_finalized":
            normalized_current = str(current_status or "").strip().lower()
            if normalized_current in {"cancelled", "terminated", "failed"}:
                return normalized_current
            return "completed"
        mapping = {
            "agent_spawned": "pending",
            "agent_prompt": "running",
            "llm_reasoning": "running",
            "llm_token": "running",
            "agent_response": "running",
            "tool_run_submitted": "running",
            "tool_run_updated": current_status,
            "steer_run_submitted": current_status,
            "steer_run_updated": current_status,
            "tool_call_started": "running",
            "tool_call": current_status,
            "shell_stream": "running",
            "agent_paused": "paused",
            "control_message": current_status,
            "agent_completed": current_status,
            "agent_cancelled": "cancelled",
            "agent_terminated": "terminated",
            "session_interrupted": "terminated",
            "session_failed": "failed",
            "protocol_error": current_status,
            "sandbox_violation": current_status,
            "child_summaries_received": current_status,
        }
        return mapping.get(event_type, current_status)

    def _event_detail(self, record: dict[str, Any], state: AgentRuntimeView) -> str:
        event_type = str(record.get("event_type", ""))
        details = record.get("payload", {})
        if event_type == "agent_spawned":
            return shorten(str(details.get("instruction", "")), width=72, placeholder="...")
        if event_type == "agent_prompt":
            return f"{self.translator.text('step_label')} {state.step_count}"
        if event_type == "tool_call_started":
            return self._describe_action(details.get("action", {}))
        if event_type == "tool_call":
            return self._describe_action(details.get("action", {}))
        if event_type == "tool_run_submitted":
            return (
                "tool_run_submitted "
                f"id={details.get('tool_run_id', '-')}, "
                f"tool={details.get('tool_name', '-')}"
            )
        if event_type == "tool_run_updated":
            return (
                "tool_run_updated "
                f"id={details.get('tool_run_id', '-')}, "
                f"status={details.get('status', '-')}"
            )
        if event_type == "steer_run_submitted":
            return (
                "steer_run_submitted "
                f"id={details.get('steer_run_id', '-')}, "
                f"from={self._steer_source_actor_label(details)}, "
                f"status={details.get('status', '-')}"
            )
        if event_type == "steer_run_updated":
            return (
                "steer_run_updated "
                f"id={details.get('steer_run_id', '-')}, "
                f"from={self._steer_source_actor_label(details)}, "
                f"status={details.get('status', '-')}"
            )
        if event_type == "child_summaries_received":
            children = details.get("children", [])
            count = len(children) if isinstance(children, list) else 0
            return f"{self.translator.text('stream_child_summaries')}={count}"
        if event_type == "agent_completed":
            return shorten(str(details.get("summary", "")), width=72, placeholder="...")
        if event_type in {"agent_cancelled", "agent_terminated"}:
            reason = str(details.get("reason", "")).strip()
            if reason:
                return shorten(reason, width=72, placeholder="...")
            if event_type == "agent_terminated":
                return self.translator.text("status_terminated")
            return self.translator.text("status_cancelled")
        if event_type == "session_finalized":
            return shorten(str(details.get("user_summary", "")), width=72, placeholder="...")
        if event_type == "project_sync_staged":
            return (
                f"{self.translator.text('sync_state_pending')} "
                f"(+{details.get('added', 0)}/~{details.get('modified', 0)}/-{details.get('deleted', 0)})"
            )
        if event_type == "project_sync_applied":
            return (
                f"{self.translator.text('sync_apply_done')} "
                f"(+{details.get('added', 0)}/~{details.get('modified', 0)}/-{details.get('deleted', 0)})"
            )
        if event_type == "project_sync_reverted":
            return (
                f"{self.translator.text('sync_undo_done')} "
                f"(removed={details.get('removed', 0)}, restored={details.get('restored', 0)})"
            )
        if event_type == "session_failed":
            return shorten(str(details.get("error", "")), width=72, placeholder="...")
        if event_type == "session_interrupted":
            return self.translator.text("session_interrupted")
        if event_type == "protocol_error":
            return shorten(str(details.get("error", "")), width=72, placeholder="...")
        if event_type == "sandbox_violation":
            return shorten(str(details.get("error", "")), width=72, placeholder="...")
        if event_type == "control_message":
            return shorten(self._control_message_stream_text(details), width=72, placeholder="...")
        if event_type == "context_compacted":
            compacted = self._format_step_range_text(details.get("step_range"))
            if compacted:
                return f"{self.translator.text('compressed_block_label')} {compacted}"
            return self.translator.text("compressed_block_label")
        if event_type == "llm_reasoning":
            return self.translator.text("stream_thinking")
        if event_type == "llm_token":
            return self.translator.text("generating")
        if event_type == "shell_stream":
            return self.translator.text("running_shell")
        return event_type

    @staticmethod
    def _normalize_step_range(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            start = int(value.get("start"))
            end = int(value.get("end"))
        except (TypeError, ValueError):
            return None
        if start <= 0 or end < start:
            return None
        return start, end

    def _format_step_range_text(self, value: Any) -> str:
        normalized = self._normalize_step_range(value)
        if normalized is None:
            return ""
        start, end = normalized
        if start == end:
            return f"{self.translator.text('step_label')} {start}"
        return f"{self.translator.text('step_label')} {start}-{end}"

    def _apply_context_metrics(
        self,
        state: AgentRuntimeView,
        details: dict[str, Any],
        *,
        event_type: str,
    ) -> None:
        context_tokens = self._coerce_non_negative_int(details.get("current_context_tokens"))
        if context_tokens is not None:
            state.current_context_tokens = context_tokens
        context_limit = self._coerce_non_negative_int(details.get("context_limit_tokens"))
        if context_limit is not None:
            state.context_limit_tokens = context_limit
        try:
            usage_ratio = float(details.get("usage_ratio"))
        except (TypeError, ValueError):
            usage_ratio = None
        if usage_ratio is not None and usage_ratio >= 0:
            state.usage_ratio = usage_ratio
        compression_count = self._coerce_non_negative_int(details.get("compression_count"))
        if compression_count is not None:
            state.compression_count = compression_count
        self._apply_context_summary_metrics(state, details)
        self._apply_usage_metrics(state, details)
        last_compacted = self._format_step_range_text(details.get("last_compacted_step_range"))
        if last_compacted:
            state.last_compacted_step_range = last_compacted
        if event_type == "context_compacted":
            compacted = self._normalize_step_range(details.get("step_range"))
            if compacted is not None and compacted not in state.compacted_step_ranges:
                state.compacted_step_ranges.append(compacted)
                state.compacted_step_ranges.sort()
                state.last_compacted_step_range = self._format_step_range_text(
                    {"start": compacted[0], "end": compacted[1]}
                )
            after_tokens = self._coerce_non_negative_int(details.get("context_tokens_after"))
            if after_tokens is not None:
                state.current_context_tokens = after_tokens
            limit_tokens = self._coerce_non_negative_int(details.get("context_limit_tokens"))
            if limit_tokens is not None:
                state.context_limit_tokens = limit_tokens
            if state.context_limit_tokens > 0:
                state.usage_ratio = (
                    round(state.current_context_tokens / state.context_limit_tokens, 4)
                    if state.current_context_tokens >= 0
                    else 0.0
                )

    def _describe_action(self, action: Any) -> str:
        if not isinstance(action, dict):
            return str(action)
        action_type = str(action.get("type", "action"))
        suffix = self._tool_call_id_suffix(action)
        if action_type == "shell":
            command = shorten(str(action.get("command", "")), width=54, placeholder="...")
            if command:
                return f"shell{suffix}: {command}"
            return f"shell{suffix}"
        if action_type == "wait_time":
            return f"wait_time{suffix}(seconds={action.get('seconds', '-')})"
        if action_type == "spawn_agent":
            name = shorten(str(action.get("name", "worker")), width=24, placeholder="...")
            instruction = shorten(
                str(action.get("instruction", "")),
                width=56,
                placeholder="...",
            )
            if instruction:
                return f"spawn_agent{suffix}(name={name}, instruction={instruction})"
            return f"spawn_agent{suffix}(name={name})"
        if action_type == "cancel_agent":
            return f"cancel_agent{suffix}(agent_id={action.get('agent_id', '-')})"
        if action_type == "list_agent_runs":
            return f"list_agent_runs{suffix}()"
        if action_type == "get_agent_run":
            return f"get_agent_run{suffix}(agent_id={action.get('agent_id', '-')})"
        if action_type == "list_tool_runs":
            cursor = str(action.get("cursor", "")).strip()
            if cursor:
                return f"list_tool_runs{suffix}(status={action.get('status', '-')}, cursor=...)"
            return f"list_tool_runs{suffix}(status={action.get('status', '-')})"
        if action_type == "get_tool_run":
            return f"get_tool_run{suffix}(tool_run_id={action.get('tool_run_id', '-')})"
        if action_type == "wait_run":
            tool_run_id = str(action.get("tool_run_id", "")).strip()
            agent_id = str(action.get("agent_id", "")).strip()
            if tool_run_id:
                return f"wait_run{suffix}(tool_run_id={tool_run_id})"
            return f"wait_run{suffix}(agent_id={agent_id or '-'})"
        if action_type == "cancel_tool_run":
            return f"cancel_tool_run{suffix}(tool_run_id={action.get('tool_run_id', '-')})"
        if action_type == "finish":
            return f"finish{suffix}(status={action.get('status', '-')})"
        return f"{action_type}{suffix}"

    @staticmethod
    def _tool_call_id_suffix(action: dict[str, Any]) -> str:
        tool_call_id = str(action.get("_tool_call_id", "")).strip()
        if not tool_call_id:
            return ""
        return f" (tool_call_id={tool_call_id})"

    @staticmethod
    def _tool_call_id_from_action(action: Any) -> str:
        if not isinstance(action, dict):
            return ""
        return str(action.get("_tool_call_id", "")).strip()

    def _register_tool_run_timeline_event(self, record: dict[str, Any]) -> None:
        event_type = str(record.get("event_type", "")).strip()
        if event_type not in {
            "tool_call_started",
            "tool_call",
            "tool_run_submitted",
            "tool_run_updated",
        }:
            return
        payload = record.get("payload")
        details = payload if isinstance(payload, dict) else {}
        action = details.get("action")
        action_payload = action if isinstance(action, dict) else {}
        call_id = self._tool_call_id_from_action(action_payload)
        tool_run_id = str(details.get("tool_run_id", "")).strip()

        if event_type == "tool_run_submitted" and tool_run_id and call_id:
            self._tool_run_call_id_to_run_id[call_id] = tool_run_id
        if not tool_run_id and call_id:
            tool_run_id = str(self._tool_run_call_id_to_run_id.get(call_id, "")).strip()
        if not tool_run_id and event_type in {"tool_call_started", "tool_call"}:
            tool_run_id = str(action_payload.get("tool_run_id", "")).strip()
        if not tool_run_id:
            return

        timeline = self._tool_run_timeline_by_run_id.setdefault(tool_run_id, [])
        entry = {
            "timestamp": str(record.get("timestamp", "")),
            "event_type": event_type,
            "phase": str(record.get("phase", "")),
            "agent_id": str(record.get("agent_id", "")),
            "payload": details,
        }
        last = timeline[-1] if timeline else None
        if not (
            isinstance(last, dict)
            and str(last.get("timestamp", "")) == str(entry["timestamp"])
            and str(last.get("event_type", "")) == str(entry["event_type"])
            and str(last.get("phase", "")) == str(entry["phase"])
        ):
            timeline.append(entry)
            if len(timeline) > 300:
                del timeline[:-300]

        self._apply_tool_run_event_to_snapshot(tool_run_id, event_type=event_type, details=details)
        if self._tool_runs_detail_open_run_id == tool_run_id:
            self._refresh_open_tool_run_detail()

    def _apply_tool_run_event_to_snapshot(
        self,
        tool_run_id: str,
        *,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        run = self._find_tool_run_in_snapshot(tool_run_id)
        if run is None:
            return
        if event_type == "tool_run_submitted":
            tool_name = str(details.get("tool_name", "")).strip()
            if tool_name:
                run["tool_name"] = tool_name
            if not str(run.get("status", "")).strip():
                run["status"] = "queued"
        elif event_type == "tool_run_updated":
            status = str(details.get("status", "")).strip()
            if status:
                run["status"] = status
            if "error" in details:
                error_text = str(details.get("error", "")).strip()
                run["error"] = error_text or None
            result_payload = details.get("result")
            if isinstance(result_payload, dict):
                run["result"] = result_payload
            started_at = str(details.get("started_at", "")).strip()
            if started_at:
                run["started_at"] = started_at
            completed_at = str(details.get("completed_at", "")).strip()
            if completed_at:
                run["completed_at"] = completed_at

    def _find_tool_run_in_snapshot(self, tool_run_id: str) -> dict[str, Any] | None:
        if not isinstance(self.tool_runs_snapshot, dict):
            return None
        runs = self.tool_runs_snapshot.get("tool_runs", [])
        if not isinstance(runs, list):
            return None
        target = str(tool_run_id).strip()
        if not target:
            return None
        for run in runs:
            if not isinstance(run, dict):
                continue
            if str(run.get("id", "")).strip() == target:
                return run
        return None

    @staticmethod
    def _snapshot_tool_run(run: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(json.dumps(run))
        except Exception:
            return dict(run)

    def _tool_runs_current_runs(self) -> list[dict[str, Any]]:
        if not isinstance(self.tool_runs_snapshot, dict):
            return []
        runs = self.tool_runs_snapshot.get("tool_runs", [])
        if not isinstance(runs, list):
            return []
        return [run for run in runs if isinstance(run, dict)]

    def _sync_tool_run_selection(self, runs: list[dict[str, Any]]) -> None:
        run_ids = [str(run.get("id", "")).strip() for run in runs if str(run.get("id", "")).strip()]
        if not run_ids:
            self.tool_runs_selected_run_id = None
            return
        if self.tool_runs_selected_run_id in run_ids:
            return
        self.tool_runs_selected_run_id = run_ids[0]

    def _select_tool_run_relative(self, offset: int) -> None:
        runs = self._tool_runs_current_runs()
        run_ids = [str(run.get("id", "")).strip() for run in runs if str(run.get("id", "")).strip()]
        if not run_ids:
            self.tool_runs_selected_run_id = None
            return
        current_id = str(self.tool_runs_selected_run_id or "").strip()
        if current_id in run_ids:
            current_index = run_ids.index(current_id)
        else:
            current_index = 0
        next_index = (current_index + offset) % len(run_ids)
        self.tool_runs_selected_run_id = run_ids[next_index]

    def _selected_tool_run(self) -> dict[str, Any] | None:
        selected_id = str(self.tool_runs_selected_run_id or "").strip()
        if not selected_id:
            return None
        return self._find_tool_run_in_snapshot(selected_id)

    def _open_selected_tool_run_detail(self) -> None:
        run = self._selected_tool_run()
        if run is None:
            return
        run_id = str(run.get("id", "")).strip()
        if not run_id:
            return
        self._tool_runs_detail_open_run_id = run_id
        run_snapshot = self._snapshot_tool_run(run)
        self._tool_runs_detail_run_snapshot = run_snapshot
        timeline = list(self._tool_run_timeline_by_run_id.get(run_id, []))
        screen = self.screen
        if isinstance(screen, ToolRunDetailScreen):
            screen.refresh_detail(
                translator=self.translator,
                run_id=run_id,
                run_snapshot=run_snapshot,
                timeline_entries=timeline,
            )
            return
        self.push_screen(
            ToolRunDetailScreen(
                translator=self.translator,
                run_id=run_id,
                run_snapshot=run_snapshot,
                timeline_entries=timeline,
            ),
            self._on_tool_run_detail_dismissed,
        )

    def _on_tool_run_detail_dismissed(self, _: object | None = None) -> None:
        self._tool_runs_detail_open_run_id = None
        self._tool_runs_detail_run_snapshot = None

    def _refresh_open_tool_run_detail(self) -> None:
        run_id = str(self._tool_runs_detail_open_run_id or "").strip()
        if not run_id:
            return
        screen = self.screen
        if not isinstance(screen, ToolRunDetailScreen):
            return
        current_run = self._find_tool_run_in_snapshot(run_id)
        if current_run is not None:
            self._tool_runs_detail_run_snapshot = self._snapshot_tool_run(current_run)
        screen.refresh_detail(
            translator=self.translator,
            run_id=run_id,
            run_snapshot=self._tool_runs_detail_run_snapshot,
            timeline_entries=list(self._tool_run_timeline_by_run_id.get(run_id, [])),
        )

    @staticmethod
    def _is_multiagent_action_type(action_type: str) -> bool:
        return action_type in {"spawn_agent", "cancel_agent", "list_agent_runs"}

    def _action_stream_kind(self, action: Any, *, stage: str = "call") -> str:
        default = "tool_return" if stage == "return" else "tool_call"
        if not isinstance(action, dict):
            return default
        action_type = str(action.get("type", "")).strip()
        if self._is_multiagent_action_type(action_type):
            return "multiagent_return" if stage == "return" else "multiagent_call"
        return default

    @staticmethod
    def _extra_kind(kind: str) -> str:
        return f"{kind}_extra"

    def _control_message_stream_text(self, details: dict[str, Any]) -> str:
        kind = str(details.get("kind", "")).strip()
        content = str(details.get("content", "")).strip()
        if content and kind:
            return f"[{kind}] {content}"
        if content:
            return content
        if kind:
            return f"[{kind}]"
        return ""

    def _replace_message_stream_entries(
        self,
        records: list[dict[str, Any]],
        *,
        preserve_non_message: bool,
    ) -> None:
        preserved_entries_by_agent: dict[str, list[tuple[int, str, str]]] = {}
        for agent_id, state in self.agent_states.items():
            if preserve_non_message:
                preserved_entries_by_agent[agent_id] = self._non_message_entries(state)
            state.step_entries = {}
            state.step_order = []
            state.stream_entries = []
            state.next_message_step = 1
            state.last_message_index = -1
            state.output_tokens_total = 0
            state.is_generating = False
            state.raw_llm_buffer = ""
            state.raw_reasoning_buffer = ""
        self._apply_message_records(records)
        if preserve_non_message:
            for agent_id, entries in preserved_entries_by_agent.items():
                state = self.agent_states.get(agent_id)
                if state is None:
                    continue
                for step_number, kind, body in entries:
                    self._append_step_stream_entry(
                        state,
                        step_number,
                        kind,
                        body,
                        keep_whitespace=True,
                    )

    def _apply_message_records(self, records: list[dict[str, Any]]) -> None:
        ordered_records = sorted(
            (record for record in records if isinstance(record, dict)),
            key=lambda record: (
                str(record.get("timestamp", "")),
                str(record.get("agent_id", "")),
                self._safe_int(record.get("message_index"), default=-1),
            ),
        )
        for record in ordered_records:
            self._apply_message_record(record)

    def _apply_message_record(self, record: dict[str, Any]) -> None:
        agent_id = str(record.get("agent_id", "")).strip()
        if not agent_id:
            return
        details = {
            "agent_name": record.get("agent_name"),
            "agent_role": record.get("agent_role"),
        }
        state = self._ensure_agent_state(
            agent_id=agent_id,
            parent_agent_id=None,
            details=details,
        )
        message_index = self._safe_int(record.get("message_index"), default=-1)
        if message_index <= state.last_message_index:
            return
        hidden_from_step_view = bool(record.get("internal", False)) or bool(
            record.get("exclude_from_context_compression", False)
        )
        if hidden_from_step_view:
            state.last_message_index = max(state.last_message_index, message_index)
            return
        role = str(record.get("role", "")).strip()
        message = record.get("message")
        if not isinstance(message, dict):
            message = {}
        explicit_step = self._safe_int(record.get("step_count"), default=0)
        next_step = max(1, state.next_message_step)
        if role == "assistant":
            derived_step = next_step
        else:
            derived_step = max(1, next_step - 1)
        if explicit_step > 0:
            step_number = max(derived_step, explicit_step)
        else:
            step_number = derived_step
        self._clear_preview_entries(state, step_number=step_number)
        if role == "assistant":
            content = str(message.get("content", ""))
            reasoning = str(message.get("reasoning", "")).strip()
            tool_calls = self._normalize_message_tool_calls(message.get("tool_calls"))
            state.output_tokens_total += self._message_record_output_tokens(record)
            response_entries = self._response_stream_entries(
                content,
                reasoning=reasoning,
                tool_calls=tool_calls,
            )
            if not response_entries and content.strip():
                response_entries = [("response", content.strip())]
            for kind, body in response_entries:
                bucket = self._ensure_step_bucket(state, step_number)
                if self._step_stream_contains(bucket, kind, body):
                    continue
                self._append_step_stream_entry(state, step_number, kind, body)
            state.raw_llm_buffer = content
            if reasoning:
                state.raw_reasoning_buffer = reasoning
            state.is_generating = False
            state.next_message_step = max(state.next_message_step, step_number + 1)
            state.step_count = max(state.step_count, step_number)
        elif role == "tool":
            tool_body = str(message.get("content", "")).strip()
            tool_call_id = str(message.get("tool_call_id", "")).strip()
            lines: list[str] = []
            if tool_call_id:
                lines.append(f"tool_call_id={tool_call_id}")
            if tool_body:
                parsed_tool_body = self._parse_json_like_text(tool_body)
                if parsed_tool_body is not None:
                    lines.append(self._format_labeled_structured_value("content", parsed_tool_body))
                else:
                    lines.append(tool_body)
            tool_body = "\n".join(lines)
            normalized_body = tool_body.strip()
            if normalized_body:
                self._append_step_stream_entry(state, step_number, "tool_message", normalized_body)
                state.step_count = max(state.step_count, step_number)
        else:
            user_body = str(message.get("content", "")).strip()
            if user_body:
                self._append_step_stream_entry(state, step_number, "user_message", user_body)
                state.step_count = max(state.step_count, step_number)
        state.last_message_index = message_index

    def _non_message_entries(self, state: AgentRuntimeView) -> list[tuple[int, str, str]]:
        entries: list[tuple[int, str, str]] = []
        for step_number in state.step_order:
            for kind, body in state.step_entries.get(step_number, []):
                if self._is_non_message_stream_kind(kind):
                    entries.append((step_number, kind, body))
        return entries

    def _clear_preview_entries(self, state: AgentRuntimeView, *, step_number: int) -> None:
        if step_number not in state.step_entries:
            return
        original = state.step_entries.get(step_number, [])
        filtered = [entry for entry in original if not self._is_preview_stream_kind(entry[0])]
        if len(filtered) == len(original):
            return
        state.step_entries[step_number] = filtered
        self._sync_flat_stream_entries(state)

    def _normalize_message_tool_calls(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_tool_calls, list):
            return []
        normalized: list[dict[str, Any]] = []
        for tool_call in raw_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function_payload = tool_call.get("function")
            if isinstance(function_payload, dict):
                arguments = function_payload.get("arguments")
                parsed_arguments: dict[str, Any] | str
                if isinstance(arguments, str):
                    try:
                        parsed_arguments = extract_json_object(arguments)
                    except Exception:
                        parsed_arguments = arguments
                elif isinstance(arguments, dict):
                    parsed_arguments = arguments
                else:
                    parsed_arguments = {}
                normalized.append(
                    {
                        "id": tool_call.get("id"),
                        "name": function_payload.get("name"),
                        "arguments": parsed_arguments,
                    }
                )
                continue
            normalized.append(tool_call)
        return normalized

    @staticmethod
    def _safe_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        return parsed

    @classmethod
    def _usage_output_tokens(cls, usage: Any) -> int:
        if not isinstance(usage, dict):
            return 0
        for key in (
            "output_tokens",
            "completion_tokens",
            "assistant_tokens",
            "generated_tokens",
            "response_tokens",
        ):
            value = cls._coerce_non_negative_int(usage.get(key))
            if value is not None:
                return value
        total_tokens = cls._coerce_non_negative_int(usage.get("total_tokens"))
        prompt_tokens = cls._coerce_non_negative_int(usage.get("prompt_tokens"))
        input_tokens = cls._coerce_non_negative_int(usage.get("input_tokens"))
        baseline = prompt_tokens if prompt_tokens is not None else input_tokens
        if total_tokens is not None and baseline is not None:
            return max(0, total_tokens - baseline)
        return 0

    @classmethod
    def _message_record_output_tokens(cls, record: dict[str, Any]) -> int:
        message = record.get("message")
        response = record.get("response")
        usage_candidates: list[Any] = []
        if isinstance(response, dict):
            usage_candidates.append(response.get("usage"))
        usage_candidates.append(record.get("usage"))
        if isinstance(message, dict):
            usage_candidates.append(message.get("usage"))
        for usage in usage_candidates:
            tokens = cls._usage_output_tokens(usage)
            if tokens > 0:
                return tokens
        return 0

    def _update_stream_for_event(self, state: AgentRuntimeView, record: dict[str, Any]) -> None:
        event_type = str(record.get("event_type", ""))
        details = record.get("payload", {})
        step_number = self._event_step_number(state, details)
        if event_type == "agent_prompt":
            state.raw_llm_buffer = ""
            state.raw_reasoning_buffer = ""
            state.is_generating = False
            self._clear_preview_entries(state, step_number=step_number)
            return
        if event_type == "llm_reasoning":
            state.is_generating = True
            state.raw_reasoning_buffer += str(details.get("token", ""))
            self._append_step_stream_entry(
                state,
                step_number,
                "thinking_preview",
                str(details.get("token", "")),
                merge=True,
                keep_whitespace=True,
            )
            return
        if event_type == "llm_token":
            state.is_generating = True
            state.raw_llm_buffer += str(details.get("token", ""))
            self._append_step_stream_entry(
                state,
                step_number,
                "reply_preview",
                str(details.get("token", "")),
                merge=True,
                keep_whitespace=True,
            )
            return
        if event_type == "agent_response":
            state.is_generating = False
            state.raw_llm_buffer = str(details.get("content", ""))
            state.raw_reasoning_buffer = str(details.get("reasoning") or state.raw_reasoning_buffer)
            return
        if event_type == "agent_spawned":
            parent_text = self._live_parent_agent_text(state)
            self._append_step_stream_entry(
                state,
                step_number,
                self._extra_kind("multiagent_return"),
                f"{self.translator.text('stream_spawned_from')}: {parent_text}",
            )
            parent_id = (state.parent_agent_id or "").strip()
            if parent_id:
                parent_state = self.agent_states.get(parent_id)
                if parent_state is not None:
                    parent_step = max(parent_state.step_count, 1)
                    parent_step_entries = self._ensure_step_bucket(parent_state, parent_step)
                    spawn_result = f"spawn_agent result: child_agent_id={state.id}"
                    if not self._step_stream_contains(
                        parent_step_entries,
                        self._extra_kind("multiagent_return"),
                        spawn_result,
                    ):
                        self._append_step_stream_entry(
                            parent_state,
                            parent_step,
                            self._extra_kind("multiagent_return"),
                            spawn_result,
                        )
            return
        if event_type == "tool_call_started":
            step_entries = self._ensure_step_bucket(state, step_number)
            action = details.get("action", {})
            action_detail = self._describe_action(action)
            action_kind = self._extra_kind(self._action_stream_kind(action, stage="call"))
            if self._step_stream_contains(step_entries, action_kind, action_detail):
                return
            self._append_step_stream_entry(
                state,
                step_number,
                action_kind,
                action_detail,
            )
            return
        if event_type == "tool_call":
            step_entries = self._ensure_step_bucket(state, step_number)
            for kind, body in self._tool_call_result_entries(details):
                extra_kind = self._extra_kind(kind)
                if not self._step_stream_contains(step_entries, extra_kind, body):
                    self._append_step_stream_entry(state, step_number, extra_kind, body)
            return
        if event_type == "tool_run_submitted":
            tool_run_id = str(details.get("tool_run_id", "")).strip()
            tool_name = str(details.get("tool_name", "")).strip() or "-"
            if tool_run_id:
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("tool_call"),
                    f"tool_run submitted: id={tool_run_id}, tool={tool_name}",
                )
            return
        if event_type == "tool_run_updated":
            tool_run_id = str(details.get("tool_run_id", "")).strip()
            status = str(details.get("status", "")).strip() or "-"
            if tool_run_id:
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("tool_return"),
                    f"tool_run updated: id={tool_run_id}, status={status}",
                )
            return
        if event_type == "steer_run_submitted":
            steer_run_id = str(details.get("steer_run_id", "")).strip()
            status = str(details.get("status", "")).strip() or "-"
            source_actor = self._steer_source_actor_label(details)
            if steer_run_id:
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("control"),
                    f"steer_run submitted: id={steer_run_id}, from={source_actor}, status={status}",
                )
            return
        if event_type == "steer_run_updated":
            steer_run_id = str(details.get("steer_run_id", "")).strip()
            status = str(details.get("status", "")).strip() or "-"
            source_actor = self._steer_source_actor_label(details)
            if steer_run_id:
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("control"),
                    f"steer_run updated: id={steer_run_id}, from={source_actor}, status={status}",
                )
            return
        if event_type == "shell_stream":
            channel = (
                self._extra_kind("stderr")
                if str(record.get("phase", "")).lower() == "stderr"
                else self._extra_kind("stdout")
            )
            self._append_step_stream_entry(
                state,
                step_number,
                channel,
                str(details.get("text", "")),
                merge=True,
                keep_whitespace=True,
            )
            return
        if event_type == "control_message":
            control_text = self._control_message_stream_text(details)
            if control_text:
                control_kind = str(details.get("kind", "")).strip()
                entry_kind = (
                    "control"
                    if control_kind == "context_pressure_reminder"
                    else self._extra_kind("control")
                )
                self._append_step_stream_entry(
                    state,
                    step_number,
                    entry_kind,
                    control_text,
                )
            return
        if event_type == "context_compacted":
            compacted = self._normalize_step_range(details.get("step_range"))
            if compacted is not None:
                start_step, end_step = compacted
                if end_step > start_step:
                    body = (
                        f"{self.translator.text('compressed_block_label')} "
                        f"({self.translator.text('step_label')} {start_step}-{end_step})"
                    )
                else:
                    body = (
                        f"{self.translator.text('compressed_block_label')} "
                        f"({self.translator.text('step_label')} {start_step})"
                    )
            else:
                body = self.translator.text("compressed_block_label")
            self._append_step_stream_entry(
                state,
                step_number,
                self._extra_kind("summary"),
                body,
            )
            return
        if event_type == "protocol_error":
            self._append_step_stream_entry(
                state,
                step_number,
                self._extra_kind("error"),
                str(details.get("error", "")),
            )
            return
        if event_type == "sandbox_violation":
            self._append_step_stream_entry(
                state,
                step_number,
                self._extra_kind("error"),
                str(details.get("error", "")),
            )
            return
        if event_type == "child_summaries_received":
            for summary_entry in self._child_summary_stream_entries(details):
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("multiagent_return"),
                    summary_entry,
                )
            return
        if event_type == "agent_completed":
            summary = str(details.get("summary", ""))
            if summary:
                self._append_step_stream_entry(
                    state,
                    step_number,
                    self._extra_kind("summary"),
                    summary,
                )
            return
        if event_type in {"agent_cancelled", "agent_terminated"}:
            fallback_key = (
                "status_terminated" if event_type == "agent_terminated" else "status_cancelled"
            )
            reason = str(details.get("reason", "")).strip() or self.translator.text(fallback_key)
            self._append_step_stream_entry(
                state,
                step_number,
                self._extra_kind("summary"),
                reason,
            )

    def _tool_call_result_entries(self, details: dict[str, Any]) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        action = details.get("action", {})
        action_type = str(action.get("type", "")) if isinstance(action, dict) else ""
        action_label = action_type or "tool"
        action_kind = self._action_stream_kind(action, stage="return")
        result = self._tool_call_result_payload(details)
        if action_type == "shell":
            result = dict(result)
            result.pop("command", None)
        if action_type == "spawn_agent":
            child_id = str(result.get("child_agent_id", "")).strip()
            tool_run_id = str(result.get("tool_run_id", "")).strip()
            if child_id:
                if tool_run_id:
                    entries.append(
                        (
                            "multiagent_return",
                            f"spawn_agent result: child_agent_id={child_id}, tool_run_id={tool_run_id}",
                        )
                    )
                else:
                    entries.append(("multiagent_return", f"spawn_agent result: child_agent_id={child_id}"))
        elif action_type == "cancel_agent":
            status = result.get("cancel_agent_status")
            if isinstance(status, bool):
                entries.append(("multiagent_return", f"cancel_agent result: status={status}"))
        elif action_type == "list_agent_runs":
            listed = result.get("agent_runs", [])
            if isinstance(listed, list):
                next_cursor = str(result.get("next_cursor", "")).strip()
                if next_cursor:
                    entries.append(
                        (
                            "multiagent_return",
                            f"list_agent_runs result: runs_count={len(listed)}, next_cursor=...",
                        )
                    )
                else:
                    entries.append(("multiagent_return", f"list_agent_runs result: runs_count={len(listed)}"))
        elif action_type == "wait_run":
            status = result.get("wait_run_status")
            if isinstance(status, bool):
                entries.append(("tool_return", f"wait_run result: status={status}"))
        elif action_type == "wait_time":
            status = result.get("wait_time_status")
            if isinstance(status, bool):
                entries.append(("tool_return", f"wait_time result: status={status}"))
        elif action_type == "list_tool_runs":
            listed = result.get("tool_runs", [])
            if isinstance(listed, list):
                next_cursor = str(result.get("next_cursor", "")).strip()
                if next_cursor:
                    entries.append(
                        (
                            "tool_return",
                            f"list_tool_runs result: runs_count={len(listed)}, next_cursor=...",
                        )
                    )
                else:
                    entries.append(("tool_return", f"list_tool_runs result: runs_count={len(listed)}"))
        elif action_type == "cancel_tool_run":
            final_status = str(result.get("final_status", "")).strip()
            if final_status:
                entries.append(("tool_return", f"cancel_tool_run result: status={final_status}"))
            else:
                run = result.get("tool_run")
                if isinstance(run, dict):
                    status = str(run.get("status", "-"))
                    entries.append(("tool_return", f"cancel_tool_run result: status={status}"))

        warning = str(result.get("warning", "")).strip()
        if warning:
            if self._is_multiagent_action_type(action_type):
                entries.append(("multiagent_return", f"{action_type} warning: {warning}"))
            else:
                entries.append((action_kind, warning))
        error = str(result.get("error", "")).strip()
        if error:
            if self._is_multiagent_action_type(action_type):
                entries.append(("error", f"{action_type} error: {error}"))
            else:
                entries.append(("error", error))
        if entries:
            if result:
                serialized_result = self._format_labeled_structured_value(
                    f"{action_label} result",
                    result,
                    fallback="{}",
                )
                if self._is_multiagent_action_type(action_type):
                    entries.append(("multiagent_return", serialized_result))
                else:
                    entries.append((action_kind, serialized_result))
            return entries

        preview = self._tool_call_result_preview_text(details)
        if preview:
            preview_result = self._format_labeled_structured_value(f"{action_label} result", preview)
            if self._is_multiagent_action_type(action_type):
                entries.append(("multiagent_return", preview_result))
            else:
                entries.append((action_kind, preview_result))
        return entries

    def _tool_call_result_payload(self, details: dict[str, Any]) -> dict[str, Any]:
        action = details.get("action", {})
        action_type = str(action.get("type", "")) if isinstance(action, dict) else ""
        result = details.get("result")
        if isinstance(result, dict):
            payload = dict(result)
            if action_type == "shell":
                payload.pop("command", None)
            return payload
        preview = details.get("result_preview")
        if isinstance(preview, str):
            normalized = preview.strip()
            if normalized.startswith("{"):
                with suppress(json.JSONDecodeError):
                    parsed = json.loads(normalized)
                    if isinstance(parsed, dict):
                        if action_type == "shell":
                            parsed = dict(parsed)
                            parsed.pop("command", None)
                        return parsed
        return {}

    def _tool_call_result_preview_text(self, details: dict[str, Any]) -> str:
        preview = details.get("result_preview")
        if isinstance(preview, str) and preview.strip():
            normalized = preview.strip()
            action = details.get("action", {})
            action_type = str(action.get("type", "")) if isinstance(action, dict) else ""
            if action_type == "shell" and normalized.startswith("{"):
                with suppress(json.JSONDecodeError):
                    parsed = json.loads(normalized)
                    if isinstance(parsed, dict):
                        parsed_payload = dict(parsed)
                        parsed_payload.pop("command", None)
                        return self._format_structured_value(parsed_payload)
            return self._format_structured_value(normalized)
        result = self._tool_call_result_payload(details)
        if result:
            return self._format_structured_value(result)
        return ""

    @staticmethod
    def _parse_json_like_text(text: str) -> Any | None:
        normalized = text.strip()
        if not normalized:
            return None
        if not (
            (normalized.startswith("{") and normalized.endswith("}"))
            or (normalized.startswith("[") and normalized.endswith("]"))
        ):
            return None
        with suppress(json.JSONDecodeError):
            return json.loads(normalized)
        return None

    def _format_structured_value(self, value: Any, *, fallback: str = "") -> str:
        if value is None:
            return fallback
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return fallback
            parsed = self._parse_json_like_text(normalized)
            if parsed is not None:
                with suppress(TypeError, ValueError):
                    return json.dumps(parsed, ensure_ascii=False, indent=2)
            return normalized
        with suppress(TypeError, ValueError):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    def _format_labeled_structured_value(
        self,
        label: str,
        value: Any,
        *,
        fallback: str = "",
    ) -> str:
        rendered = self._format_structured_value(value, fallback=fallback)
        if not rendered:
            return f"{label}:"
        if "\n" in rendered:
            return f"{label}:\n{rendered}"
        return f"{label}: {rendered}"

    def _child_summary_stream_entries(self, details: dict[str, Any]) -> list[str]:
        children = details.get("children")
        if not isinstance(children, list):
            return []
        entries: list[str] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            child_id = str(child.get("id", "")).strip()
            child_name = str(child.get("name", "")).strip() or child_id or "child"
            status = str(child.get("status", "")).strip()
            summary = str(child.get("summary", "")).strip()
            recommendation = str(child.get("next_recommendation", "")).strip()
            header = f"{child_name} ({child_id})" if child_id else child_name
            if status:
                header = f"{header} [{status}]"
            body = summary or self.translator.text("none_value")
            entry = f"{self.translator.text('stream_child_summaries')}: {header}: {body}"
            if recommendation:
                entry = f"{entry} | {self.translator.text('stream_next')}: {recommendation}"
            entries.append(entry)
        return entries

    def _event_step_number(self, state: AgentRuntimeView, details: dict[str, Any]) -> int:
        candidate = details.get("step_count", state.step_count)
        try:
            step_number = int(candidate)
        except (TypeError, ValueError):
            step_number = 0
        if step_number > 0:
            return step_number
        if state.step_order:
            return state.step_order[-1]
        return 1

    def _ensure_step_bucket(self, state: AgentRuntimeView, step_number: int) -> list[tuple[str, str]]:
        if step_number not in state.step_entries:
            state.step_entries[step_number] = []
            if step_number not in state.step_order:
                state.step_order.append(step_number)
        return state.step_entries[step_number]

    def _append_step_stream_entry(
        self,
        state: AgentRuntimeView,
        step_number: int,
        kind: str,
        text: str,
        *,
        merge: bool = False,
        keep_whitespace: bool = False,
    ) -> None:
        if not text:
            return
        normalized = text if keep_whitespace else text.rstrip("\n")
        if not normalized and not keep_whitespace:
            return
        if not keep_whitespace and not normalized.strip():
            return
        entries = self._ensure_step_bucket(state, step_number)
        if merge and entries and entries[-1][0] == kind:
            previous_kind, previous_text = entries[-1]
            entries[-1] = (previous_kind, previous_text + normalized)
        else:
            entries.append((kind, normalized))
        self._sync_flat_stream_entries(state)

    def _upsert_response_stream_entry(
        self,
        state: AgentRuntimeView,
        step_entries: list[tuple[str, str]],
        *,
        step_number: int,
        kind: str,
        text: str,
    ) -> bool:
        if kind not in {"thinking", "reply", "response"}:
            return False
        latest_index = self._latest_step_entry_index(step_entries, kind)
        if latest_index is None:
            return False
        existing_text = step_entries[latest_index][1]
        if not self._stream_entry_text_equivalent(existing_text, text):
            return False
        if existing_text != text:
            step_entries[latest_index] = (kind, text)
            state.step_entries[step_number] = step_entries
            self._sync_flat_stream_entries(state)
        return True

    @staticmethod
    def _latest_step_entry_index(entries: list[tuple[str, str]], kind: str) -> int | None:
        for index in range(len(entries) - 1, -1, -1):
            if entries[index][0] == kind:
                return index
        return None

    @staticmethod
    def _stream_entry_text_equivalent(existing: str, incoming: str) -> bool:
        existing_normalized = re.sub(r"\s+", " ", existing).strip()
        incoming_normalized = re.sub(r"\s+", " ", incoming).strip()
        if not existing_normalized or not incoming_normalized:
            return False
        if existing_normalized == incoming_normalized:
            return True
        if len(existing_normalized) <= len(incoming_normalized):
            shorter, longer = existing_normalized, incoming_normalized
        else:
            shorter, longer = incoming_normalized, existing_normalized
        if len(shorter) >= 32 and shorter in longer and len(shorter) / len(longer) >= 0.9:
            return True
        if len(existing_normalized) >= 48 and len(incoming_normalized) >= 48:
            return SequenceMatcher(None, existing_normalized, incoming_normalized).ratio() >= 0.985
        return False

    @staticmethod
    def _step_stream_contains(
        entries: list[tuple[str, str]],
        kind: str,
        text: str,
    ) -> bool:
        return any(entry_kind == kind and entry_text == text for entry_kind, entry_text in entries)

    def _sync_flat_stream_entries(self, state: AgentRuntimeView) -> None:
        ordered_entries: list[tuple[str, str]] = []
        for step_number in state.step_order:
            ordered_entries.extend(state.step_entries.get(step_number, []))
        state.stream_entries = ordered_entries

    def _append_stream_entry(
        self,
        state: AgentRuntimeView,
        kind: str,
        text: str,
        *,
        merge: bool = False,
    ) -> None:
        step_number = state.step_order[-1] if state.step_order else max(state.step_count, 1)
        self._append_step_stream_entry(state, step_number, kind, text, merge=merge)

    def _replace_live_llm_entries(
        self,
        state: AgentRuntimeView,
        entries: list[tuple[str, str]],
    ) -> None:
        preserved = [
            entry
            for entry in state.stream_entries
            if entry[0] not in {"thinking", "reply", "response", "generating"}
        ]
        state.stream_entries = [*entries, *preserved]

    def _response_stream_entries(
        self,
        raw_text: str,
        *,
        reasoning: str = "",
        actions: Any | None = None,
        tool_calls: Any | None = None,
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        normalized_reasoning = reasoning.strip()
        if normalized_reasoning:
            entries.append(("thinking", normalized_reasoning))

        for entry in self._action_entries_from_message(raw_text, actions=actions):
            if entry not in entries:
                entries.append(entry)
        for entry in self._tool_call_trace_entries(tool_calls):
            if entry not in entries:
                entries.append(entry)
        if not entries:
            fallback = raw_text.strip()
            if fallback:
                entries.append(("response", fallback))
        return entries

    def _action_entries_from_message(
        self,
        raw_text: str,
        *,
        actions: Any | None = None,
    ) -> list[tuple[str, str]]:
        try:
            payload = extract_json_object(raw_text)
        except Exception:
            if isinstance(actions, list):
                return self._action_entries(actions)
            fallback = raw_text.strip()
            return [("response", fallback)] if fallback else []

        entries: list[tuple[str, str]] = []
        thinking = str(payload.get("thinking", "")).strip()
        if thinking:
            entries.append(("thinking", thinking))
        action_source = actions if isinstance(actions, list) else payload.get("actions", [])
        for entry in self._action_entries(action_source):
            if entry not in entries:
                entries.append(entry)
        return entries

    def _tool_call_trace_entries(self, tool_calls: Any | None) -> list[tuple[str, str]]:
        if not isinstance(tool_calls, list):
            return []
        entries: list[tuple[str, str]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id", "")).strip()
            name = str(tool_call.get("name", "")).strip()
            if not call_id and not name:
                continue
            arguments = tool_call.get("arguments")
            argument_value = arguments if arguments is not None else {}
            label = "tool_call"
            lines: list[str] = []
            if call_id:
                lines.append(f"tool_call_id={call_id}")
            if name:
                lines.append(f"name={name}")
            lines.append(
                self._format_labeled_structured_value(
                    "arguments",
                    argument_value,
                    fallback="{}",
                )
            )
            text = "\n".join(lines)
            entries.append((label, text))
        return entries

    def _streaming_llm_entries(
        self,
        raw_text: str,
        *,
        reasoning: str = "",
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        normalized_reasoning = reasoning.strip()
        if normalized_reasoning:
            entries.append(("thinking", normalized_reasoning))
        for entry in self._streaming_entries_from_partial_response(raw_text):
            if entry not in entries:
                entries.append(entry)
        return entries

    def _action_entries(self, actions: Any) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        if not isinstance(actions, list):
            return entries
        for action in actions:
            if not isinstance(action, dict):
                continue
            entries.append((self._action_stream_kind(action, stage="call"), self._describe_action(action)))
        return entries

    def _streaming_entries_from_partial_response(self, raw_text: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        thinking = self._extract_partial_json_string(raw_text, "thinking")
        if thinking:
            entries.append(("thinking", thinking))
        for key in ("user_summary", "summary", "next_recommendation"):
            value = self._extract_partial_json_string(raw_text, key)
            if value and ("reply", value) not in entries:
                entries.append(("reply", value))
        return entries

    def _extract_partial_json_string(self, raw_text: str, key: str) -> str:
        marker = f'"{key}"'
        start = raw_text.find(marker)
        if start == -1:
            return ""
        colon = raw_text.find(":", start + len(marker))
        if colon == -1:
            return ""
        quote = raw_text.find('"', colon)
        if quote == -1:
            return ""
        chars: list[str] = []
        escaped = False
        for char in raw_text[quote + 1 :]:
            if escaped:
                chars.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(char, char))
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                break
            chars.append(char)
        return "".join(chars).strip()

    def _should_write_activity(self, record: dict[str, Any]) -> bool:
        return str(record.get("event_type", "")) not in ACTIVITY_SKIP_EVENTS

    def _format_event(self, record: dict[str, Any]) -> str:
        timestamp = str(record.get("timestamp", ""))[11:19]
        details = record.get("payload", {})
        agent_name = str(
            details.get("agent_name")
            or details.get("root_agent_name")
            or record.get("agent_id")
            or "session"
        )
        event_type = str(record.get("event_type", ""))
        if event_type == "session_started":
            return f"[{timestamp}] session started {self._short_session_id(record)} {shorten(str(details.get('task', '')), width=60, placeholder='...')}"
        if event_type == "session_resumed":
            return f"[{timestamp}] session continued {self._short_session_id(record)}"
        if event_type == "session_context_imported":
            return f"[{timestamp}] session context imported {self._short_session_id(record)}"
        if event_type == "session_finalized":
            return f"[{timestamp}] session finalized {shorten(str(details.get('user_summary', '')), width=72, placeholder='...')}"
        if event_type == "project_sync_staged":
            return (
                f"[{timestamp}] project sync staged "
                f"(+{details.get('added', 0)}/~{details.get('modified', 0)}/-{details.get('deleted', 0)})"
            )
        if event_type == "project_sync_applied":
            return (
                f"[{timestamp}] project sync applied "
                f"(+{details.get('added', 0)}/~{details.get('modified', 0)}/-{details.get('deleted', 0)})"
            )
        if event_type == "project_sync_reverted":
            return (
                f"[{timestamp}] project sync reverted "
                f"(removed={details.get('removed', 0)}, restored={details.get('restored', 0)})"
            )
        if event_type == "session_interrupted":
            return f"[{timestamp}] session interrupted"
        if event_type == "session_failed":
            return f"[{timestamp}] session failed {shorten(str(details.get('error', '')), width=72, placeholder='...')}"
        if event_type == "agent_spawned":
            return f"[{timestamp}] {agent_name} spawned {shorten(str(details.get('instruction', '')), width=60, placeholder='...')}"
        if event_type == "agent_prompt":
            return f"[{timestamp}] {agent_name} prompting step={details.get('step_count', 0)}"
        if event_type == "agent_paused":
            return f"[{timestamp}] {agent_name} paused"
        if event_type == "tool_call_started":
            return f"[{timestamp}] {agent_name} {self._describe_action(details.get('action', {}))}"
        if event_type == "tool_call":
            return f"[{timestamp}] {agent_name} finished {self._describe_action(details.get('action', {}))}"
        if event_type == "tool_run_submitted":
            return (
                f"[{timestamp}] {agent_name} tool_run submitted "
                f"id={details.get('tool_run_id', '-')}, tool={details.get('tool_name', '-')}"
            )
        if event_type == "tool_run_updated":
            return (
                f"[{timestamp}] {agent_name} tool_run updated "
                f"id={details.get('tool_run_id', '-')}, status={details.get('status', '-')}"
            )
        if event_type == "steer_run_submitted":
            return (
                f"[{timestamp}] {agent_name} steer_run submitted "
                f"id={details.get('steer_run_id', '-')}, "
                f"from={self._steer_source_actor_label(details)}, "
                f"status={details.get('status', '-')}"
            )
        if event_type == "steer_run_updated":
            return (
                f"[{timestamp}] {agent_name} steer_run updated "
                f"id={details.get('steer_run_id', '-')}, "
                f"from={self._steer_source_actor_label(details)}, "
                f"status={details.get('status', '-')}"
            )
        if event_type == "child_summaries_received":
            children = details.get("children", [])
            count = len(children) if isinstance(children, list) else 0
            return (
                f"[{timestamp}] {agent_name} "
                f"{self.translator.text('stream_child_summaries')}={count}"
            )
        if event_type == "agent_completed":
            return f"[{timestamp}] {agent_name} completed {shorten(str(details.get('summary', '')), width=60, placeholder='...')}"
        if event_type in {"agent_cancelled", "agent_terminated"}:
            reason = shorten(str(details.get("reason", "")).strip(), width=60, placeholder="...")
            status_key = (
                "status_terminated" if event_type == "agent_terminated" else "status_cancelled"
            )
            if reason:
                return (
                    f"[{timestamp}] {agent_name} {self.translator.text(status_key)} {reason}"
                )
            return f"[{timestamp}] {agent_name} {self.translator.text(status_key)}"
        if event_type == "control_message":
            return f"[{timestamp}] {agent_name} control {self._control_message_stream_text(details)}"
        if event_type == "protocol_error":
            return f"[{timestamp}] {agent_name} protocol error"
        if event_type == "sandbox_violation":
            return f"[{timestamp}] {agent_name} sandbox violation"
        return f"[{timestamp}] {agent_name} {event_type}"

    def _short_session_id(self, record: dict[str, Any]) -> str:
        session_id = str(record.get("session_id", ""))
        return session_id[:8] if session_id else "-"

    def _render_all(self) -> None:
        if self._static_text_dirty:
            self._refresh_static_text()
        self._sync_task_input_default()
        self._update_diff_tab_availability()
        self._render_status_panel()
        self._render_tool_runs_panel()
        self._render_steer_runs_panel()
        self._render_diff_panel()
        self._queue_agent_panel_refresh()

    def _invalidate_diff_preview_cache(self) -> None:
        self._diff_preview_cache_key = None
        self._diff_preview_cache_data = None
        self._diff_render_cache_key = None

    def _cycle_tool_runs_filter(self) -> None:
        options = ["all", "pending", "running", "completed", "failed", "cancelled"]
        try:
            index = options.index(self.tool_runs_filter)
        except ValueError:
            self.tool_runs_filter = options[0]
            return
        self.tool_runs_filter = options[(index + 1) % len(options)]

    def _cycle_tool_runs_group(self) -> None:
        options = ["agent", "tool", "status"]
        try:
            index = options.index(self.tool_runs_group_by)
        except ValueError:
            self.tool_runs_group_by = options[0]
            return
        self.tool_runs_group_by = options[(index + 1) % len(options)]

    def _tool_runs_filter_statuses(self) -> list[str] | None:
        if self.tool_runs_filter == "pending":
            return ["queued", "running"]
        if self.tool_runs_filter == "running":
            return ["running"]
        if self.tool_runs_filter == "completed":
            return ["completed"]
        if self.tool_runs_filter == "failed":
            return ["failed"]
        if self.tool_runs_filter == "cancelled":
            return ["cancelled"]
        return None

    def _tool_runs_filter_label(self) -> str:
        mapping = {
            "all": "tool_runs_filter_all",
            "pending": "tool_runs_filter_pending",
            "running": "tool_runs_filter_running",
            "completed": "tool_runs_filter_completed",
            "failed": "tool_runs_filter_failed",
            "cancelled": "tool_runs_filter_cancelled",
        }
        return self.translator.text(mapping.get(self.tool_runs_filter, "tool_runs_filter_all"))

    def _tool_runs_group_label(self) -> str:
        mapping = {
            "agent": "tool_runs_group_agent",
            "tool": "tool_runs_group_tool",
            "status": "tool_runs_group_status",
        }
        return self.translator.text(mapping.get(self.tool_runs_group_by, "tool_runs_group_agent"))

    def _refresh_tool_runs_data(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            self.tool_runs_snapshot = {"tool_runs": [], "next_cursor": None}
            self.tool_runs_metrics_snapshot = None
            self.tool_runs_status_message = self.translator.text("tool_runs_no_session")
            self.tool_runs_selected_run_id = None
            self._tool_runs_detail_open_run_id = None
            self._tool_runs_detail_run_snapshot = None
            self._tool_runs_dirty = False
            self._tool_runs_cache_key = None
            return
        cache_key = (session_id, self.tool_runs_filter)
        if not self._tool_runs_dirty and self._tool_runs_cache_key == cache_key:
            return

        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            self.tool_runs_snapshot = orchestrator.list_tool_runs_page(
                session_id,
                status=self._tool_runs_filter_statuses(),
                limit=500,
                cursor=None,
            )
            self.tool_runs_metrics_snapshot = orchestrator.tool_run_metrics(session_id)
            candidate = self.tool_runs_snapshot.get("tool_runs", [])
            runs = candidate if isinstance(candidate, list) else []
            self._sync_tool_run_selection([run for run in runs if isinstance(run, dict)])
            count = len(runs)
            self.tool_runs_status_message = (
                f"{self.translator.text('tool_runs_count')}: {count}"
            )
            self._tool_runs_cache_key = cache_key
        except Exception as exc:
            self.tool_runs_snapshot = {"tool_runs": [], "next_cursor": None}
            self.tool_runs_metrics_snapshot = None
            self.tool_runs_status_message = str(exc)
            self.tool_runs_selected_run_id = None
            self._tool_runs_cache_key = None
        finally:
            self._tool_runs_dirty = False
            if created_local:
                del orchestrator

    def _render_tool_runs_panel(self) -> None:
        panel_title = self._query_optional("#tool_runs_panel_title", Static)
        status_widget = self._query_optional("#tool_runs_status", Static)
        content_widget = self._query_optional("#tool_runs_content", Static)
        refresh_button = self._query_optional("#tool_runs_refresh_button", Button)
        filter_button = self._query_optional("#tool_runs_filter_button", Button)
        group_button = self._query_optional("#tool_runs_group_button", Button)
        previous_button = self._query_optional("#tool_runs_prev_button", Button)
        next_button = self._query_optional("#tool_runs_next_button", Button)
        detail_button = self._query_optional("#tool_runs_detail_button", Button)
        if (
            panel_title is None
            or status_widget is None
            or content_widget is None
            or refresh_button is None
            or filter_button is None
            or group_button is None
            or previous_button is None
            or next_button is None
            or detail_button is None
        ):
            return
        panel_title.update(self.translator.text("tool_runs_tab_title"))
        refresh_button.label = self.translator.text("reload")
        filter_button.label = (
            f"{self.translator.text('tool_runs_filter')}: {self._tool_runs_filter_label()}"
        )
        group_button.label = (
            f"{self.translator.text('tool_runs_group')}: {self._tool_runs_group_label()}"
        )
        previous_button.label = self.translator.text("tool_runs_prev_button")
        next_button.label = self.translator.text("tool_runs_next_button")
        detail_button.label = self.translator.text("tool_runs_detail_button")

        self._refresh_tool_runs_data()
        status_widget.update(self.tool_runs_status_message)
        runs = []
        if isinstance(self.tool_runs_snapshot, dict):
            candidate = self.tool_runs_snapshot.get("tool_runs", [])
            if isinstance(candidate, list):
                runs = [run for run in candidate if isinstance(run, dict)]
        self._sync_tool_run_selection(runs)

        rendered = Text()
        session_id = self._active_session_id()
        if not session_id:
            previous_button.disabled = True
            next_button.disabled = True
            detail_button.disabled = True
            rendered.append(self.translator.text("tool_runs_no_session"), style="dim")
            content_widget.update(rendered)
            return

        metrics = self.tool_runs_metrics_snapshot if isinstance(self.tool_runs_metrics_snapshot, dict) else {}
        status_counts = metrics.get("status_counts")
        if not isinstance(status_counts, dict):
            status_counts = {}
        duration = metrics.get("duration_ms")
        if not isinstance(duration, dict):
            duration = {}
        failure_rate = float(metrics.get("failure_rate", 0.0) or 0.0)
        failure_or_cancel_rate = float(metrics.get("failure_or_cancel_rate", 0.0) or 0.0)

        pending_count = int(status_counts.get("queued", 0)) + int(status_counts.get("running", 0))
        rendered.append(f"{self.translator.text('session_id')}: ", style="bold cyan")
        rendered.append(f"{session_id}\n")
        rendered.append(f"{self.translator.text('tool_runs_metric_total')}: ", style="bold cyan")
        rendered.append(f"{int(metrics.get('total_runs', 0) or 0)}")
        rendered.append(" | ")
        rendered.append(f"{self.translator.text('tool_runs_metric_pending')}: ", style="bold cyan")
        rendered.append(f"{pending_count}")
        rendered.append(" | ")
        rendered.append(f"{self.translator.text('tool_runs_metric_terminal')}: ", style="bold cyan")
        rendered.append(f"{int(metrics.get('terminal_runs', 0) or 0)}\n")
        rendered.append(f"{self.translator.text('tool_runs_metric_failure_rate')}: ", style="bold cyan")
        rendered.append(f"{failure_rate * 100:.2f}%")
        rendered.append(" | ")
        rendered.append(
            f"{self.translator.text('tool_runs_metric_failure_or_cancel_rate')}: ",
            style="bold cyan",
        )
        rendered.append(f"{failure_or_cancel_rate * 100:.2f}%\n")
        rendered.append(f"{self.translator.text('tool_runs_metric_p50')}: ", style="bold cyan")
        rendered.append(self._tool_run_duration_text(duration.get("p50")))
        rendered.append(" | ")
        rendered.append(f"{self.translator.text('tool_runs_metric_p95')}: ", style="bold cyan")
        rendered.append(self._tool_run_duration_text(duration.get("p95")))
        rendered.append(" | ")
        rendered.append(f"{self.translator.text('tool_runs_metric_p99')}: ", style="bold cyan")
        rendered.append(self._tool_run_duration_text(duration.get("p99")))

        if not runs:
            previous_button.disabled = True
            next_button.disabled = True
            detail_button.disabled = True
            rendered.append("\n\n")
            rendered.append(self.translator.text("tool_runs_empty"), style="dim")
            content_widget.update(rendered)
            self._refresh_open_tool_run_detail()
            return

        previous_button.disabled = False
        next_button.disabled = False
        detail_button.disabled = self._selected_tool_run() is None

        grouped = self._group_tool_runs(runs)
        for group_key, group_runs in grouped:
            rendered.append("\n\n")
            rendered.append(str(group_key), style="bold")
            rendered.append(f" ({len(group_runs)})", style="dim")
            for run in group_runs:
                run_id = str(run.get("id", "-"))
                tool_name = str(run.get("tool_name", "-"))
                agent_id = str(run.get("agent_id", "-"))
                status = str(run.get("status", "unknown"))
                duration_text = self._tool_run_duration_text(tool_run_duration_ms(run))
                is_selected = run_id == self.tool_runs_selected_run_id
                rendered.append("\n")
                rendered.append("  ▶ " if is_selected else "  • ", style="bold green" if is_selected else "dim")
                rendered.append(shorten(run_id, width=26, placeholder="..."), style="cyan")
                rendered.append(" ")
                rendered.append(status, style=self._tool_run_status_style(status))
                rendered.append(" ")
                rendered.append(shorten(tool_name, width=28, placeholder="..."), style="yellow")
                rendered.append(
                    f" | agent={shorten(agent_id, width=18, placeholder='...')}",
                    style="dim",
                )
                rendered.append(
                    f" | {self.translator.text('tool_runs_duration')}={duration_text}",
                    style="dim",
                )
        content_widget.update(rendered)
        self._refresh_open_tool_run_detail()

    def _group_tool_runs(self, runs: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            if self.tool_runs_group_by == "tool":
                key = str(run.get("tool_name", "-"))
            elif self.tool_runs_group_by == "status":
                key = str(run.get("status", "-"))
            else:
                key = str(run.get("agent_id", "-"))
            grouped.setdefault(key, []).append(run)
        return sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))

    def _tool_run_duration_text(self, duration_ms: Any) -> str:
        try:
            value = int(duration_ms)
        except (TypeError, ValueError):
            return "-"
        if value < 1_000:
            return f"{value}ms"
        seconds = value / 1_000
        if seconds < 60:
            precision = 2 if seconds < 10 else 1
            return f"{seconds:.{precision}f}s"
        minutes = int(seconds // 60)
        remaining = int(round(seconds % 60))
        return f"{minutes}m{remaining}s"

    def _tool_run_status_style(self, status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "completed":
            return "green"
        if normalized == "failed":
            return "red"
        if normalized == "cancelled":
            return "yellow"
        if normalized in {"running", "queued"}:
            return "cyan"
        return "dim"

    def _cycle_steer_runs_filter(self) -> None:
        options = ["all", "waiting", "completed", "cancelled"]
        try:
            index = options.index(self.steer_runs_filter)
        except ValueError:
            self.steer_runs_filter = options[0]
            return
        self.steer_runs_filter = options[(index + 1) % len(options)]

    def _cycle_steer_runs_group(self) -> None:
        options = ["agent", "status"]
        try:
            index = options.index(self.steer_runs_group_by)
        except ValueError:
            self.steer_runs_group_by = options[0]
            return
        self.steer_runs_group_by = options[(index + 1) % len(options)]

    def _steer_runs_filter_statuses(self) -> list[str] | None:
        if self.steer_runs_filter == "waiting":
            return ["waiting"]
        if self.steer_runs_filter == "completed":
            return ["completed"]
        if self.steer_runs_filter == "cancelled":
            return ["cancelled"]
        return None

    def _steer_runs_filter_label(self) -> str:
        mapping = {
            "all": "steer_runs_filter_all",
            "waiting": "steer_runs_filter_waiting",
            "completed": "steer_runs_filter_completed",
            "cancelled": "steer_runs_filter_cancelled",
        }
        return self.translator.text(mapping.get(self.steer_runs_filter, "steer_runs_filter_all"))

    def _steer_runs_group_label(self) -> str:
        mapping = {
            "agent": "steer_runs_group_agent",
            "status": "steer_runs_group_status",
        }
        return self.translator.text(mapping.get(self.steer_runs_group_by, "steer_runs_group_agent"))

    def _refresh_steer_runs_data(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            self.steer_runs_snapshot = {"steer_runs": [], "next_cursor": None}
            self.steer_runs_metrics_snapshot = None
            self.steer_runs_status_message = self.translator.text("steer_runs_no_session")
            self._steer_runs_dirty = False
            self._steer_runs_cache_key = None
            return
        cache_key = (session_id, self.steer_runs_filter)
        if not self._steer_runs_dirty and self._steer_runs_cache_key == cache_key:
            return

        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            self.steer_runs_snapshot = orchestrator.list_steer_runs_page(
                session_id,
                status=self._steer_runs_filter_statuses(),
                limit=500,
                cursor=None,
            )
            self.steer_runs_metrics_snapshot = orchestrator.steer_run_metrics(session_id)
            candidate = self.steer_runs_snapshot.get("steer_runs", [])
            runs = candidate if isinstance(candidate, list) else []
            self.steer_runs_status_message = (
                f"{self.translator.text('steer_runs_count')}: {len(runs)}"
            )
            self._steer_runs_cache_key = cache_key
        except Exception as exc:
            self.steer_runs_snapshot = {"steer_runs": [], "next_cursor": None}
            self.steer_runs_metrics_snapshot = None
            self.steer_runs_status_message = str(exc)
            self._steer_runs_cache_key = None
        finally:
            self._steer_runs_dirty = False
            if created_local:
                del orchestrator

    def _render_steer_runs_panel(self) -> None:
        tabs = self._query_optional("#main_tabs", TabbedContent)
        if tabs is not None and tabs.active != "steer_runs_tab":
            return
        panel_title = self._query_optional("#steer_runs_panel_title", Static)
        status_widget = self._query_optional("#steer_runs_status", Static)
        content_widget = self._query_optional("#steer_runs_content", Vertical)
        refresh_button = self._query_optional("#steer_runs_refresh_button", Button)
        filter_button = self._query_optional("#steer_runs_filter_button", Button)
        group_button = self._query_optional("#steer_runs_group_button", Button)
        if (
            panel_title is None
            or status_widget is None
            or content_widget is None
            or refresh_button is None
            or filter_button is None
            or group_button is None
        ):
            return
        panel_title.update(self.translator.text("steer_runs_tab_title"))
        refresh_button.label = self.translator.text("reload")
        filter_button.label = (
            f"{self.translator.text('steer_runs_filter')}: {self._steer_runs_filter_label()}"
        )
        group_button.label = (
            f"{self.translator.text('steer_runs_group')}: {self._steer_runs_group_label()}"
        )

        self._refresh_steer_runs_data()
        status_widget.update(self.steer_runs_status_message)
        session_id = self._active_session_id()

        metrics = (
            self.steer_runs_metrics_snapshot
            if isinstance(self.steer_runs_metrics_snapshot, dict)
            else {}
        )
        runs: list[dict[str, Any]] = []
        if isinstance(self.steer_runs_snapshot, dict):
            candidate = self.steer_runs_snapshot.get("steer_runs", [])
            if isinstance(candidate, list):
                runs = [run for run in candidate if isinstance(run, dict)]

        widgets: list[Widget] = []
        if not session_id:
            widgets.append(Static(self.translator.text("steer_runs_no_session"), classes="empty-state"))
            self.call_later(
                lambda: asyncio.create_task(self._replace_panel_children(content_widget, widgets))
            )
            return

        counts = metrics.get("status_counts")
        if not isinstance(counts, dict):
            counts = {}
        metrics_text = Text()
        metrics_text.append(f"{self.translator.text('session_id')}: ", style="bold cyan")
        metrics_text.append(f"{session_id}\n")
        metrics_text.append(f"{self.translator.text('steer_runs_metric_total')}: ", style="bold cyan")
        metrics_text.append(str(int(metrics.get("total_runs", 0) or 0)))
        metrics_text.append(" | ")
        metrics_text.append(f"{self.translator.text('steer_runs_metric_waiting')}: ", style="bold cyan")
        metrics_text.append(str(int(counts.get("waiting", 0) or 0)))
        metrics_text.append(" | ")
        metrics_text.append(f"{self.translator.text('steer_runs_metric_completed')}: ", style="bold cyan")
        metrics_text.append(str(int(counts.get("completed", 0) or 0)))
        metrics_text.append(" | ")
        metrics_text.append(f"{self.translator.text('steer_runs_metric_cancelled')}: ", style="bold cyan")
        metrics_text.append(str(int(counts.get("cancelled", 0) or 0)))
        widgets.append(Static(metrics_text))

        if not runs:
            widgets.append(Static(self.translator.text("steer_runs_empty"), classes="empty-state"))
            self.call_later(
                lambda: asyncio.create_task(self._replace_panel_children(content_widget, widgets))
            )
            return

        for group_key, group_runs in self._group_steer_runs(runs):
            group_widgets: list[Widget] = [
                Static(f"{group_key} ({len(group_runs)})", classes="panel-title")
            ]
            for run in group_runs:
                run_id = str(run.get("id", "")).strip()
                agent_id = str(run.get("agent_id", "")).strip() or "-"
                status = str(run.get("status", "")).strip() or "-"
                source = str(run.get("source", "")).strip() or "-"
                source_actor = self._steer_source_actor_label(run)
                created_at = str(run.get("created_at", "")).strip() or "-"
                delivery = self._steer_run_delivery_label(run)
                header_text = Text()
                header_text.append(shorten(run_id, width=28, placeholder="..."), style="bold cyan")
                header_text.append("  ")
                header_text.append(
                    self._status_label(status),
                    style=self._steer_run_status_style(status),
                )
                meta_text = Text()
                meta_text.append(
                    f"{self.translator.text('steer_runs_target_agent')}: ",
                    style="bold cyan",
                )
                meta_text.append(agent_id)
                meta_text.append("\n")
                meta_text.append(
                    f"{self.translator.text('steer_runs_source_actor')}: ",
                    style="bold cyan",
                )
                meta_text.append(source_actor)
                meta_text.append("\n")
                meta_text.append(
                    f"{self.translator.text('steer_runs_source_channel')}: ",
                    style="bold cyan",
                )
                meta_text.append(source)
                meta_text.append("\n")
                meta_text.append(
                    f"{self.translator.text('steer_runs_created_at')}: ",
                    style="bold cyan",
                )
                meta_text.append(created_at)
                meta_text.append("\n")
                meta_text.append(
                    f"{self.translator.text('steer_runs_inserted')}: ",
                    style="bold cyan",
                )
                meta_text.append(delivery)
                content_text = Text()
                content_text.append(f"{self.translator.text('message')}:\n", style="bold cyan")
                content_value = str(run.get("content", "")).strip() or self.translator.text("none_value")
                content_text.append(content_value, style="white")
                row_children: list[Widget] = [
                    Static(header_text, classes="steer-run-card-header"),
                    Static(meta_text, classes="steer-run-card-meta"),
                    Static(content_text, classes="steer-run-card-content"),
                ]
                if status.strip().lower() == "waiting" and run_id:
                    row_children.append(
                        Horizontal(
                            SteerRunCancelButton(
                                self.translator.text("steer_runs_cancel_button"),
                                steer_run_id=run_id,
                                id=f"steer-run-cancel-{self._widget_safe_id(run_id)}",
                            ),
                            classes="steer-run-card-actions",
                        )
                    )
                group_widgets.append(Vertical(*row_children, classes="steer-run-row"))
            widgets.append(Vertical(*group_widgets, classes="steer-run-group"))

        self.call_later(
            lambda: asyncio.create_task(self._replace_panel_children(content_widget, widgets))
        )

    def _group_steer_runs(self, runs: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            if self.steer_runs_group_by == "status":
                key = str(run.get("status", "-"))
            else:
                key = str(run.get("agent_id", "-"))
            grouped.setdefault(key, []).append(run)
        return sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))

    def _steer_source_actor_label(self, record: dict[str, Any]) -> str:
        source_agent_id = str(record.get("source_agent_id", "")).strip()
        source_agent_name = str(record.get("source_agent_name", "")).strip()
        if not source_agent_id or source_agent_id == "user":
            return "用户" if self.locale == "zh" else "user"
        if source_agent_name:
            return f"{source_agent_name} ({source_agent_id})"
        return source_agent_id

    def _steer_run_delivery_label(self, record: dict[str, Any]) -> str:
        raw_step = record.get("delivered_step")
        try:
            delivered_step = int(raw_step) if raw_step is not None else None
        except (TypeError, ValueError):
            delivered_step = None
        if delivered_step is not None and delivered_step > 0:
            return f"{self.translator.text('step_label')} {delivered_step}"
        status = str(record.get("status", "")).strip().lower()
        if status == "waiting":
            return self.translator.text("steer_runs_pending_delivery")
        if status == "cancelled":
            return self.translator.text("steer_runs_cancelled_before_delivery")
        if status == "completed":
            return self.translator.text("steer_runs_delivery_unknown")
        return self.translator.text("none_value")

    @staticmethod
    def _steer_run_status_style(status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "completed":
            return "green"
        if normalized == "cancelled":
            return "yellow"
        if normalized == "waiting":
            return "cyan"
        return "dim"

    def _render_diff_panel(self) -> None:
        diff_content = self._query_optional("#diff_content", Static)
        if diff_content is None:
            return
        rendered = self._diff_preview_text()
        render_key = (
            rendered.plain,
            str(rendered.style or ""),
            tuple((span.start, span.end, str(span.style)) for span in rendered.spans),
        )
        if render_key == self._diff_render_cache_key:
            return
        self._diff_render_cache_key = render_key
        diff_content.update(rendered)

    def _diff_preview_text(self) -> Text:
        session_id = self._active_session_id()
        if not session_id:
            return Text(self.translator.text("diff_no_session"), style="dim")
        if self._is_direct_workspace_mode():
            return Text(self.translator.text("diff_disabled_direct"), style="dim")
        state = self.project_sync_state
        if not state:
            return Text(self.translator.text("diff_no_staged_changes"), style="dim")
        status = str(state.get("status", "none"))
        if status == "disabled":
            return Text(self.translator.text("diff_disabled_direct"), style="dim")
        if status == "none":
            return Text(self.translator.text("diff_no_staged_changes"), style="dim")

        cache_key = json.dumps(
            {
                "session_id": session_id,
                "status": status,
                "added": state.get("added", []),
                "modified": state.get("modified", []),
                "deleted": state.get("deleted", []),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        if cache_key != self._diff_preview_cache_key:
            try:
                self._diff_preview_cache_data = self._load_project_sync_preview(session_id)
            except Exception as exc:
                self._diff_preview_cache_data = {
                    "error": f"{self.translator.text('diff_preview_failed')}: {exc}"
                }
            self._diff_preview_cache_key = cache_key
        preview = self._diff_preview_cache_data or {}
        error = str(preview.get("error", "")).strip()
        if error:
            return Text(error, style="red")
        return self._render_project_sync_preview(session_id, preview)

    def _load_project_sync_preview(self, session_id: str) -> dict[str, Any]:
        orchestrator = self.orchestrator
        created_local = False
        if orchestrator is None:
            orchestrator = self._create_orchestrator(
                self.project_dir or Path.cwd(),
                locale=self.locale,
                app_dir=self.app_dir,
            )
            created_local = True
        try:
            self.app_dir = orchestrator.app_dir
            return orchestrator.project_sync_preview(session_id, max_files=80, max_chars=200_000)
        finally:
            if created_local:
                del orchestrator

    def _render_project_sync_preview(self, session_id: str, preview: dict[str, Any]) -> Text:
        rendered = Text()
        self._append_diff_summary_line(rendered, self.translator.text("session_id"), session_id)
        self._append_diff_summary_line(
            rendered,
            self.translator.text("project_dir"),
            str(preview.get("project_dir", self.project_dir or "-")),
        )
        self._append_diff_summary_line(
            rendered,
            self.translator.text("project_sync"),
            self._project_sync_status_text(),
            value_style=self._project_sync_status_style(),
        )
        self._append_diff_summary_line(
            rendered,
            self.translator.text("diff_changed_files"),
            (
                f"+{int(preview.get('added_count', 0))}/"
                f"~{int(preview.get('modified_count', 0))}/"
                f"-{int(preview.get('deleted_count', 0))}"
            ),
            value_style="bold",
        )
        last_operation = self._last_project_sync_operation_text()
        if last_operation:
            rendered.append(last_operation, style="dim")
            rendered.append("\n")
        last_error = str((self.project_sync_state or {}).get("last_error", "")).strip()
        if last_error:
            self._append_diff_summary_line(
                rendered,
                self.translator.text("stream_error"),
                last_error,
                value_style="red",
            )
        files = list(preview.get("files", []))
        if str(preview.get("status", "none")) == "none" or not files:
            rendered.append("\n")
            rendered.append(self.translator.text("diff_no_staged_changes"), style="dim")
            return rendered

        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            path = str(file_info.get("path", ""))
            change = str(file_info.get("change_type", "modified"))
            patch = str(file_info.get("patch", "")).rstrip("\n")
            rendered.append("\n\n")
            rendered.append(path, style="bold")
            rendered.append(" ")
            rendered.append(f"[{self._diff_change_label(change)}]", style=self._diff_change_style(change))
            rendered.append("\n")
            if bool(file_info.get("is_binary", False)):
                rendered.append(self._binary_diff_message(file_info, change), style="yellow")
                continue
            if patch:
                self._append_patch_render(rendered, patch)
            else:
                rendered.append(self.translator.text("diff_patch_empty"), style="dim")
        if bool(preview.get("truncated", False)):
            rendered.append("\n\n")
            rendered.append(self.translator.text("diff_preview_truncated"), style="yellow")
        return rendered

    def _append_diff_summary_line(
        self,
        rendered: Text,
        label: str,
        value: str,
        *,
        value_style: str = "",
    ) -> None:
        rendered.append(f"{label}: ", style="bold cyan")
        rendered.append(value, style=value_style)
        rendered.append("\n")

    def _append_patch_render(self, rendered: Text, patch: str) -> None:
        lines = patch.splitlines()
        for index, line in enumerate(lines):
            rendered.append(line, style=self._diff_line_style(line))
            if index < len(lines) - 1:
                rendered.append("\n")

    def _project_sync_status_style(self) -> str:
        status = str((self.project_sync_state or {}).get("status", "none"))
        if status == "pending":
            return "yellow"
        if status == "applied":
            return "green"
        if status == "reverted":
            return "cyan"
        if status == "error":
            return "red"
        return "dim"

    def _diff_change_label(self, change_type: str) -> str:
        key = {
            "added": "diff_change_added",
            "modified": "diff_change_modified",
            "deleted": "diff_change_deleted",
        }.get(change_type)
        return self.translator.text(key) if key else change_type

    def _diff_change_style(self, change_type: str) -> str:
        if change_type == "added":
            return "bold green"
        if change_type == "deleted":
            return "bold red"
        return "bold yellow"

    def _diff_line_style(self, line: str) -> str:
        if line.startswith("@@"):
            return "bold cyan"
        if line.startswith("+++ ") or line.startswith("--- "):
            return "bold white"
        if line.startswith("+"):
            return "green"
        if line.startswith("-"):
            return "red"
        if line.startswith(" "):
            return "dim"
        return ""

    def _binary_diff_message(self, file_info: dict[str, Any], change_type: str) -> str:
        message_key = {
            "added": "diff_binary_added",
            "modified": "diff_binary_modified",
            "deleted": "diff_binary_deleted",
        }.get(change_type, "diff_binary_modified")
        message = self.translator.text(message_key)
        before_size = file_info.get("before_size")
        after_size = file_info.get("after_size")
        if before_size is None and after_size is None:
            return message
        size_label = self.translator.text("diff_binary_size")
        if before_size is not None and after_size is not None:
            return (
                f"{message} {size_label}: "
                f"{self._format_diff_size(int(before_size))} -> {self._format_diff_size(int(after_size))}"
            )
        size_value = after_size if after_size is not None else before_size
        return f"{message} {size_label}: {self._format_diff_size(int(size_value))}"

    def _format_diff_size(self, size_bytes: int) -> str:
        return f"{size_bytes} {self.translator.text('diff_bytes_unit')}"

    def _last_project_sync_operation_text(self) -> str:
        operation = self.last_project_sync_operation
        if not operation:
            return ""
        project_dir = str(operation.get("project_dir", "")).strip() or str(self.project_dir or "-")
        operation_type = str(operation.get("operation", ""))
        if operation_type == "apply":
            return (
                f"{self.translator.text('diff_last_sync')}: {self.translator.text('apply')} "
                f"| {self.translator.text('project_dir')}: {project_dir} "
                f"| +{int(operation.get('added', 0))}/~{int(operation.get('modified', 0))}/-{int(operation.get('deleted', 0))}"
            )
        if operation_type == "undo":
            message = (
                f"{self.translator.text('diff_last_sync')}: {self.translator.text('undo')} "
                f"| {self.translator.text('project_dir')}: {project_dir} "
                f"| removed={int(operation.get('removed', 0))}, restored={int(operation.get('restored', 0))}"
            )
            missing = list(operation.get("missing_backups", []))
            if missing:
                message += f" | missing backups: {', '.join(str(item) for item in missing)}"
            return message
        return ""

    def _query_optional[WidgetT: Widget](self, selector: str, expect_type: type[WidgetT]) -> WidgetT | None:
        try:
            return self.query_one(selector, expect_type)
        except NoMatches:
            return None

    def _refresh_static_text(self) -> None:
        locale_label = self._query_optional("#locale_label", Static)
        model_label = self._query_optional("#model_label", Static)
        root_agent_name_label = self._query_optional("#root_agent_name_label", Static)
        task_label = self._query_optional("#task_label", Static)
        task_input = self._query_optional("#task_input", TextArea)
        model_input = self._query_optional("#model_input", Input)
        root_agent_name_input = self._query_optional("#root_agent_name_input", Input)
        overview_title = self._query_optional("#overview_panel_title", Static)
        live_title = self._query_optional("#live_panel_title", Static)
        tool_runs_title = self._query_optional("#tool_runs_panel_title", Static)
        steer_runs_title = self._query_optional("#steer_runs_panel_title", Static)
        diff_title = self._query_optional("#diff_panel_title", Static)
        config_title = self._query_optional("#config_panel_title", Static)
        config_effect_notes = self._query_optional("#config_effect_notes", Static)
        main_tabs = self._query_optional("#main_tabs", TabbedContent)
        locale_en_button = self._query_optional("#locale_en_button", Button)
        locale_zh_button = self._query_optional("#locale_zh_button", Button)
        run_button = self._query_optional("#run_button", Button)
        terminal_button = self._query_optional("#terminal_button", Button)
        apply_button = self._query_optional("#apply_button", Button)
        undo_button = self._query_optional("#undo_button", Button)
        reconfigure_button = self._query_optional("#reconfigure_button", Button)
        interrupt_button = self._query_optional("#interrupt_button", Button)
        tool_runs_refresh_button = self._query_optional("#tool_runs_refresh_button", Button)
        tool_runs_filter_button = self._query_optional("#tool_runs_filter_button", Button)
        tool_runs_group_button = self._query_optional("#tool_runs_group_button", Button)
        tool_runs_prev_button = self._query_optional("#tool_runs_prev_button", Button)
        tool_runs_next_button = self._query_optional("#tool_runs_next_button", Button)
        tool_runs_detail_button = self._query_optional("#tool_runs_detail_button", Button)
        steer_runs_refresh_button = self._query_optional("#steer_runs_refresh_button", Button)
        steer_runs_filter_button = self._query_optional("#steer_runs_filter_button", Button)
        steer_runs_group_button = self._query_optional("#steer_runs_group_button", Button)
        config_save_button = self._query_optional("#config_save_button", Button)
        config_reload_button = self._query_optional("#config_reload_button", Button)
        if any(
            widget is None
            for widget in (
                locale_label,
                model_label,
                root_agent_name_label,
                task_label,
                task_input,
                model_input,
                root_agent_name_input,
                overview_title,
                live_title,
                tool_runs_title,
                steer_runs_title,
                diff_title,
                config_title,
                config_effect_notes,
                locale_en_button,
                locale_zh_button,
                run_button,
                terminal_button,
                apply_button,
                undo_button,
                reconfigure_button,
                interrupt_button,
                tool_runs_refresh_button,
                tool_runs_filter_button,
                tool_runs_group_button,
                tool_runs_prev_button,
                tool_runs_next_button,
                tool_runs_detail_button,
                steer_runs_refresh_button,
                steer_runs_filter_button,
                steer_runs_group_button,
                config_save_button,
                config_reload_button,
            )
        ):
            return

        locale_label.update(self.translator.text("locale"))
        model_label.update(self.translator.text("model_input_label"))
        root_agent_name_label.update(self.translator.text("root_agent_name_label"))
        task_label.update(self.translator.text("task"))
        task_input.border_title = self.translator.text("task_input")
        model_input.placeholder = self.translator.text("model_input_placeholder")
        root_agent_name_input.placeholder = self.translator.text("root_agent_name_placeholder")
        self._ensure_model_input_value()
        overview_title.update(self.translator.text("overview_title"))
        live_title.update(self.translator.text("live_output_title"))
        tool_runs_title.update(self.translator.text("tool_runs_tab_title"))
        steer_runs_title.update(self.translator.text("steer_runs_tab_title"))
        diff_title.update(self.translator.text("diff_panel_title"))
        config_title.update(self.translator.text("config_panel_title"))
        self._render_config_path()
        self._render_config_notice()
        config_effect_notes.update(
            (
                f"{self.translator.text('config_effect_title')}:\n"
                f"1. {self.translator.text('config_effect_next_session')}\n"
                f"2. {self.translator.text('config_effect_running_session')}"
            )
        )
        if main_tabs is not None:
            with suppress(Exception):
                main_tabs.get_tab("monitor_tab").label = self.translator.text("monitor_tab_title")
            with suppress(Exception):
                main_tabs.get_tab("agents_tab").label = self.translator.text("agents_tab_title")
            with suppress(Exception):
                main_tabs.get_tab("tool_runs_tab").label = self.translator.text("tool_runs_tab_title")
            with suppress(Exception):
                main_tabs.get_tab("steer_runs_tab").label = self.translator.text("steer_runs_tab_title")
            with suppress(Exception):
                main_tabs.get_tab("diff_tab").label = self.translator.text("diff_tab_title")
            with suppress(Exception):
                main_tabs.get_tab("config_tab").label = self.translator.text("config_tab_title")
        locale_en_button.label = self._compact_locale_button_label("en")
        locale_zh_button.label = self._compact_locale_button_label("zh")
        run_button.label = self.translator.text("run")
        terminal_button.label = self.translator.text("terminal")
        apply_button.label = self.translator.text("apply")
        undo_button.label = self.translator.text("undo")
        reconfigure_button.label = self.translator.text("reconfigure")
        interrupt_button.label = self.translator.text("interrupt")
        tool_runs_refresh_button.label = self.translator.text("reload")
        tool_runs_filter_button.label = (
            f"{self.translator.text('tool_runs_filter')}: {self._tool_runs_filter_label()}"
        )
        tool_runs_group_button.label = (
            f"{self.translator.text('tool_runs_group')}: {self._tool_runs_group_label()}"
        )
        tool_runs_prev_button.label = self.translator.text("tool_runs_prev_button")
        tool_runs_next_button.label = self.translator.text("tool_runs_next_button")
        tool_runs_detail_button.label = self.translator.text("tool_runs_detail_button")
        steer_runs_refresh_button.label = self.translator.text("reload")
        steer_runs_filter_button.label = (
            f"{self.translator.text('steer_runs_filter')}: {self._steer_runs_filter_label()}"
        )
        steer_runs_group_button.label = (
            f"{self.translator.text('steer_runs_group')}: {self._steer_runs_group_label()}"
        )
        config_save_button.label = self.translator.text("save")
        config_reload_button.label = self.translator.text("reload")
        locale_en_button.variant = "primary" if self.locale == "en" else "default"
        locale_zh_button.variant = "primary" if self.locale == "zh" else "default"
        self._update_config_controls()
        self._static_text_dirty = False

    def _set_button_label(self, selector: str, label: str) -> None:
        button = self._query_optional(selector, Button)
        if button is not None:
            button.label = label

    @staticmethod
    def _compact_locale_button_label(locale: str) -> str:
        normalized = str(locale).strip().lower()
        if normalized == "en":
            return "EN"
        if normalized == "zh":
            return "中文"
        return normalized.upper() or "?"

    def _render_status_panel(self) -> None:
        status_panel = self._query_optional("#status_panel", Static)
        if status_panel is None:
            return
        focus = self.agent_states.get(self.current_focus_agent_id or "")
        config = self._launch_config()
        panel_width = max(status_panel.size.width or self.size.width, 48)
        detail_label = self.translator.text("summary") if self.current_summary else self.translator.text("message")
        detail_text = self.current_summary or self.status_message or "-"
        config_line = self._fit_status_line(
            [
                f"{self.translator.text('configuration_title')}: {self._config_status_label(config)}",
                (
                    f"{self.translator.text('workspace_mode_label')}: "
                    f"{self._workspace_mode_label(config.session_mode)}"
                ),
                (
                    f"{self.translator.text('sandbox_backend_label')}: "
                    f"{self._sandbox_backend_label(config.sandbox_backend)}"
                ),
                f"{self.translator.text('project_dir')}: {self._config_project_dir_text()}",
                (
                    f"{self.translator.text('session_id')}: "
                    f"{self._display_session_id(self.configured_resume_session_id, empty_key='unset_value')}"
                ),
                f"{self.translator.text('project_sync')}: {self._project_sync_status_text()}",
                f"{self.translator.text('available_actions')}: {self._config_actions_text(config)}",
            ],
            width=panel_width,
        )
        runtime_line = self._fit_status_line(
            [
                (
                    f"{self.translator.text('task_id')}: "
                    f"{self._display_session_id(self.current_session_id, empty_key='pending_value')}"
                ),
                f"{self.translator.text('session_status')}: {self._session_status_label(self.current_session_status)}",
                (
                    f"{self.translator.text('current_focus')}: "
                    f"{focus.name if focus else self.translator.text('none_value')}"
                ),
            ],
            width=panel_width,
        )
        detail_line = shorten(
            (
                f"{self.translator.text('task')}: {self.current_task or '-'} | "
                f"{detail_label}: {detail_text}"
            ),
            width=panel_width,
            placeholder="...",
        )
        lines = [config_line, runtime_line, detail_line]
        if self.size.height <= 24:
            lines = [config_line, shorten(f"{runtime_line} | {detail_line}", width=panel_width, placeholder="...")]
        rendered = "\n".join(lines)
        if rendered == self._status_panel_cache_text:
            return
        self._status_panel_cache_text = rendered
        status_panel.update(rendered)

    def _fit_status_line(self, segments: list[str], *, width: int) -> str:
        return shorten(" | ".join(segment for segment in segments if segment), width=width, placeholder="...")

    def _apply_responsive_layout(self) -> None:
        if not self.query("#control_row"):
            return
        width = self.size.width
        height = self.size.height
        stacked_buttons = width < 54 and height >= 24
        stacked_model = width < 72
        stacked_task = width < 66

        control_row = self.query_one("#control_row", Vertical)
        model_row = self._query_optional("#model_row", Horizontal)
        task_row = self._query_optional("#task_row", Horizontal)
        buttons = self.query_one("#buttons", Horizontal)
        footer = self.query_one(Footer)
        live_tree = self._query_optional("#live_tree", Vertical)

        control_row.styles.layout = "vertical"
        if model_row is not None:
            model_row.styles.layout = "vertical" if stacked_model else "horizontal"
            model_row.styles.height = "auto" if stacked_model else 3
        if task_row is not None:
            task_row.styles.layout = "vertical" if stacked_task else "horizontal"
        buttons.styles.layout = "vertical" if stacked_buttons else "horizontal"
        if live_tree is not None:
            live_tree.styles.width = "100%"
            live_tree.styles.height = "1fr"
        footer.display = height >= 25

    def _update_task_input_height(self) -> None:
        task_input = self._query_optional("#task_input", TextArea)
        if task_input is None:
            return
        wrapped_lines = task_input.document.line_count
        if task_input.size.width > 2:
            with suppress(Exception):
                wrapped_lines = max(wrapped_lines, int(task_input.wrapped_document.height))
        # Reserve space for TextArea chrome so entering the second content line
        # immediately increases the visible editable rows.
        target_height = max(TASK_INPUT_MIN_HEIGHT, min(TASK_INPUT_MAX_HEIGHT, wrapped_lines + 2))
        task_input.styles.height = target_height

    def _default_task_input_value(self) -> str:
        return str(self.translator.text("task_input_default_value") or "").strip()

    def _set_task_input_text(self, text: str, *, seeded_default: str | None) -> None:
        task_input = self._query_optional("#task_input", TextArea)
        self._task_input_seeded_default = seeded_default
        if task_input is None:
            return
        if task_input.text == text:
            return
        self._task_input_programmatic_update = True
        try:
            task_input.load_text(text)
        finally:
            self._task_input_programmatic_update = False
        self._update_task_input_height()

    def _sync_task_input_default(self, *, locale_switched: bool = False) -> None:
        task_input = self._query_optional("#task_input", TextArea)
        if task_input is None:
            return
        current_text = str(task_input.text or "")
        runtime_task = str(self.current_task or "")
        seeded_default = self._task_input_seeded_default
        if runtime_task.strip():
            if not current_text.strip() or (
                seeded_default is not None and current_text == seeded_default
            ):
                self._set_task_input_text(runtime_task, seeded_default=None)
            else:
                self._task_input_seeded_default = None
            return
        localized_default = self._default_task_input_value()
        if not localized_default:
            return
        if not current_text.strip():
            self._set_task_input_text(localized_default, seeded_default=localized_default)
            return
        if seeded_default is not None and current_text == seeded_default:
            if locale_switched or current_text != localized_default:
                self._set_task_input_text(localized_default, seeded_default=localized_default)

    def _tree_sort_key(self, state: AgentRuntimeView) -> tuple[int, str, str]:
        role_rank = 0 if state.role == "root" else 1
        return (role_rank, state.name.lower(), state.id)

    def _queue_agent_panel_refresh(self) -> None:
        if not self.query("#overview_scroll") or not self.query("#live_scroll"):
            return
        self._panel_refresh_dirty = True
        if self._panel_refresh_scheduled:
            return
        self._panel_refresh_scheduled = True
        self.call_later(self._run_panel_refresh_pass)

    async def _run_panel_refresh_pass(self) -> None:
        try:
            while self._panel_refresh_dirty:
                self._panel_refresh_dirty = False
                wait_seconds = self._panel_refresh_interval_seconds - (
                    asyncio.get_running_loop().time() - self._last_panel_refresh_time
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                try:
                    await self._refresh_agent_panels()
                    self._last_panel_refresh_time = asyncio.get_running_loop().time()
                except asyncio.CancelledError:
                    self._log_diagnostic("panel_refresh_cancelled", level="warning")
                    if getattr(self, "_closing", False) or getattr(self, "_closed", False):
                        break
                    continue
                except Exception as exc:
                    self._log_diagnostic("panel_refresh_failed", level="error", error=exc)
                    break
        finally:
            self._panel_refresh_scheduled = False
            if self._panel_refresh_dirty:
                self._queue_agent_panel_refresh()

    async def _refresh_agent_panels(self) -> None:
        if not self.query("#overview_scroll") or not self.query("#live_scroll"):
            return

        overview_scroll = self.query_one("#overview_scroll", VerticalScroll)
        live_scroll = self.query_one("#live_scroll", VerticalScroll)
        overview_content = self._query_optional("#overview_content", Vertical)
        live_content = self._query_optional("#live_content", Vertical)
        if overview_content is None or live_content is None:
            return

        overview_scroll_y = overview_scroll.scroll_y
        live_scroll_y = live_scroll.scroll_y
        live_max_scroll_y = live_scroll.max_scroll_y
        follow_live = live_scroll.is_vertical_scroll_end or live_scroll.max_scroll_y <= 0

        with self.batch_update():
            overview_signature = self._overview_panel_structure_signature()
            live_signature = self._live_panel_structure_signature()
            overview_structure_changed = False
            live_structure_changed = False
            live_updated = False

            if overview_signature != self._overview_structure_signature:
                await self._replace_panel_children(overview_content, self._build_overview_widgets())
                self._overview_structure_signature = overview_signature
                overview_structure_changed = True
                self._sync_overview_render_fingerprints()
            else:
                self._update_overview_widgets_in_place()

            if live_signature != self._live_structure_signature:
                await self._replace_panel_children(live_content, self._build_live_widgets())
                self._live_structure_signature = live_signature
                live_structure_changed = True
                live_updated = True
                self._sync_live_render_fingerprints()
            else:
                live_updated = await self._update_live_widgets_in_place()

            live_scroll_grew = live_scroll.max_scroll_y != live_max_scroll_y

            if overview_structure_changed:
                overview_scroll.scroll_to(y=overview_scroll_y, animate=False, immediate=True)
            if live_structure_changed:
                if follow_live:
                    live_scroll.scroll_end(animate=False, immediate=True)
                else:
                    live_scroll.scroll_to(y=live_scroll_y, animate=False, immediate=True)
            elif follow_live and live_updated and live_scroll_grew:
                live_scroll.scroll_end(animate=False, immediate=True)

    async def _replace_panel_children(self, container: Widget, widgets: list[Widget]) -> None:
        await container.remove_children()
        await container.mount(*widgets)

    def _sync_overview_render_fingerprints(self) -> None:
        self._overview_render_fingerprints = {
            state.id: self._overview_render_fingerprint(state)
            for state in self.agent_states.values()
        }

    def _sync_live_render_fingerprints(self) -> None:
        visible_ids = self._visible_live_agent_ids()
        self._live_agent_render_fingerprints = {}
        self._live_step_render_fingerprints = {}
        self._live_step_entry_render_fingerprints = {}
        for agent_id in visible_ids:
            state = self.agent_states.get(agent_id)
            if state is None:
                continue
            self._live_agent_render_fingerprints[state.id] = self._live_agent_render_fingerprint(state)
            step_order = self._effective_step_order(state)
            latest_step = step_order[-1] if step_order else 0
            for step_number in step_order:
                self._live_step_render_fingerprints[(state.id, step_number)] = self._live_step_render_fingerprint(
                    state,
                    step_number,
                    latest_step,
                )
                entries = self._entries_for_step(state, step_number)
                if not entries:
                    self._live_step_entry_render_fingerprints[(state.id, step_number, -1)] = (
                        self._empty_step_entry_render_fingerprint()
                    )
                    continue
                for index in range(len(entries)):
                    self._live_step_entry_render_fingerprints[(state.id, step_number, index)] = (
                        self._live_step_entry_render_fingerprint(entries, index)
                    )

    def _overview_render_fingerprint(self, state: AgentRuntimeView) -> tuple[Any, ...]:
        instruction_text = state.instruction or self.translator.text("none_value")
        summary_text = self._overview_summary_text(state)
        return (
            self._overview_agent_title(state),
            state.id in self.overview_collapsed_agent_ids,
            self._is_overview_instruction_collapsed(state.id),
            self._is_overview_summary_collapsed(state.id),
            state.model,
            instruction_text,
            summary_text,
        )

    def _live_agent_render_fingerprint(self, state: AgentRuntimeView) -> tuple[Any, ...]:
        return (
            self._live_agent_title(state),
            state.id in self.live_collapsed_agent_ids,
            self.locale,
            state.status,
            state.step_count,
            state.output_tokens_total,
            state.current_context_tokens,
            state.context_limit_tokens,
            state.usage_ratio,
            state.last_usage_input_tokens,
            state.last_usage_output_tokens,
            state.last_usage_cache_read_tokens,
            state.last_usage_cache_write_tokens,
            state.last_usage_total_tokens,
            state.compression_count,
            state.keep_pinned_messages,
            state.summary_version,
            state.context_latest_summary,
            state.summarized_until_message_index,
            state.last_compacted_step_range,
            state.model,
            state.last_phase,
            state.last_detail,
            self._live_parent_agent_text(state),
            self._live_child_agents_text(state),
        )

    def _live_step_render_fingerprint(
        self,
        state: AgentRuntimeView,
        step_number: int,
        latest_step: int,
    ) -> tuple[Any, ...]:
        return (
            self._live_step_title(state, step_number),
            self._is_live_step_collapsed(state.id, step_number, latest_step),
            tuple(self._entries_for_step(state, step_number)),
        )

    def _overview_panel_structure_signature(self) -> tuple[Any, ...]:
        ordered = sorted(
            self.agent_states.values(),
            key=lambda state: self.stream_agent_order.index(state.id)
            if state.id in self.stream_agent_order
            else len(self.stream_agent_order),
        )
        return tuple((state.id, state.parent_agent_id) for state in ordered)

    def _live_panel_structure_signature(self) -> tuple[Any, ...]:
        visible_ids = self._visible_live_agent_ids()
        signature: list[tuple[str, str | None, tuple[int, ...]]] = []
        for agent_id in visible_ids:
            state = self.agent_states.get(agent_id)
            if state is None:
                continue
            signature.append((state.id, state.parent_agent_id, tuple(self._effective_step_order(state))))
        return tuple(signature)

    def _update_overview_widgets_in_place(self) -> bool:
        changed = False
        active_ids = set(self.agent_states.keys())
        for stale_agent_id in list(self._overview_render_fingerprints.keys()):
            if stale_agent_id not in active_ids:
                self._overview_render_fingerprints.pop(stale_agent_id, None)

        for state in self.agent_states.values():
            fingerprint = self._overview_render_fingerprint(state)
            previous = self._overview_render_fingerprints.get(state.id)
            if previous == fingerprint:
                continue

            changed = True
            self._overview_render_fingerprints[state.id] = fingerprint
            (
                title,
                agent_collapsed,
                instruction_collapsed,
                summary_collapsed,
                _model_text,
                instruction_text,
                summary_text,
            ) = fingerprint
            agent_key = self._widget_safe_id(state.id)
            agent_widget = self._query_optional(f"#wf-agent-{agent_key}", AgentCollapsible)
            if agent_widget is not None and (
                previous is None or previous[0] != title or previous[1] != agent_collapsed
            ):
                agent_widget.title = title
                agent_widget.collapsed = agent_collapsed

            instruction_section = self._query_optional(
                f"#wf-agent-{agent_key}-instruction",
                AgentSectionCollapsible,
            )
            if instruction_section is not None and (
                previous is None or previous[2] != instruction_collapsed
            ):
                instruction_section.collapsed = instruction_collapsed

            summary_section = self._query_optional(
                f"#wf-agent-{agent_key}-summary",
                AgentSectionCollapsible,
            )
            if summary_section is not None and (
                previous is None or previous[3] != summary_collapsed
            ):
                summary_section.collapsed = summary_collapsed

            instruction_body = self._query_optional(
                f"#wf-agent-{agent_key}-instruction-body",
                Static,
            )
            if instruction_body is not None and (previous is None or previous[5] != instruction_text):
                instruction_body.update(self._section_body_text(instruction_text, style="yellow"))

            summary_body = self._query_optional(
                f"#wf-agent-{agent_key}-summary-body",
                Static,
            )
            if summary_body is not None and (previous is None or previous[6] != summary_text):
                summary_body.update(self._section_body_text(summary_text, style="cyan"))
        return changed

    async def _update_live_widgets_in_place(self) -> bool:
        changed = False
        visible_ids = self._visible_live_agent_ids()
        visible_set = set(visible_ids)
        for stale_agent_id in list(self._live_agent_render_fingerprints.keys()):
            if stale_agent_id not in visible_set:
                self._live_agent_render_fingerprints.pop(stale_agent_id, None)

        valid_step_keys: set[tuple[str, int]] = set()
        for agent_id in visible_ids:
            state = self.agent_states.get(agent_id)
            if state is None:
                continue
            for step_number in self._effective_step_order(state):
                valid_step_keys.add((state.id, step_number))
        for stale_key in list(self._live_step_render_fingerprints.keys()):
            if stale_key not in valid_step_keys:
                self._live_step_render_fingerprints.pop(stale_key, None)
        for stale_entry_key in list(self._live_step_entry_render_fingerprints.keys()):
            if stale_entry_key[:2] not in valid_step_keys:
                self._live_step_entry_render_fingerprints.pop(stale_entry_key, None)

        for agent_id in visible_ids:
            state = self.agent_states.get(agent_id)
            if state is None:
                continue
            agent_fingerprint = self._live_agent_render_fingerprint(state)
            previous_agent = self._live_agent_render_fingerprints.get(state.id)
            if previous_agent != agent_fingerprint:
                changed = True
                self._live_agent_render_fingerprints[state.id] = agent_fingerprint
            agent_key = self._widget_safe_id(state.id)
            agent_widget = self._query_optional(f"#live-agent-{agent_key}", AgentCollapsible)
            if agent_widget is not None and (
                previous_agent is None
                or previous_agent[0] != agent_fingerprint[0]
                or previous_agent[1] != agent_fingerprint[1]
            ):
                agent_widget.title = agent_fingerprint[0]
                agent_widget.collapsed = agent_fingerprint[1]

            status_body = self._query_optional(f"#live-agent-{agent_key}-status-body", Static)
            if status_body is not None and (
                previous_agent is None or previous_agent[2:] != agent_fingerprint[2:]
            ):
                status_body.update(self._live_agent_status_body(state))

            steer_button = self._query_optional(
                f"#live-agent-{agent_key}-steer-button",
                AgentSteerButton,
            )
            if steer_button is not None:
                steer_button.label = self.translator.text("steer_button")
                steer_button.target_agent_id = state.id
            terminate_button = self._query_optional(
                f"#live-agent-{agent_key}-terminate-button",
                AgentTerminateButton,
            )
            if terminate_button is not None:
                terminate_button.label = self.translator.text("terminate_button")
                terminate_button.target_agent_id = state.id
            copy_name_button = self._query_optional(
                f"#live-agent-{agent_key}-copy-name-button",
                AgentCopyButton,
            )
            if copy_name_button is not None:
                copy_name_button.label = self.translator.text("copy_agent_name_button")
                copy_name_button.copy_value = state.name
                copy_name_button.copy_kind = "name"
            copy_id_button = self._query_optional(
                f"#live-agent-{agent_key}-copy-id-button",
                AgentCopyButton,
            )
            if copy_id_button is not None:
                copy_id_button.label = self.translator.text("copy_agent_id_button")
                copy_id_button.copy_value = state.id
                copy_id_button.copy_kind = "id"
            jump_row = self._query_optional(f"#live-agent-{agent_key}-jump-row", Horizontal)
            if jump_row is not None:
                await self._sync_live_agent_jump_buttons(jump_row, state, agent_key)

            step_order = self._effective_step_order(state)
            latest_step = step_order[-1] if step_order else 0
            for step_number in step_order:
                step_key = (state.id, step_number)
                step_fingerprint = self._live_step_render_fingerprint(state, step_number, latest_step)
                previous_step = self._live_step_render_fingerprints.get(step_key)
                if previous_step == step_fingerprint:
                    continue
                changed = True
                self._live_step_render_fingerprints[step_key] = step_fingerprint
                step_widget = self._query_optional(
                    f"#live-agent-{agent_key}-step-{step_number}",
                    LiveStepCollapsible,
                )
                if step_widget is not None and (
                    previous_step is None
                    or previous_step[0] != step_fingerprint[0]
                    or previous_step[1] != step_fingerprint[1]
                ):
                    step_widget.title = step_fingerprint[0]
                    step_widget.collapsed = step_fingerprint[1]
                step_body = self._query_optional(
                    f"#live-agent-{agent_key}-step-{step_number}-body",
                    Vertical,
                )
                if step_body is not None and (previous_step is None or previous_step[2] != step_fingerprint[2]):
                    await self._sync_live_step_body_widgets(step_body, state, step_number, agent_key)
        return changed

    def _render_overview_panel_content(self) -> Text:
        roots = [
            state
            for state in self.agent_states.values()
            if not state.parent_agent_id or state.parent_agent_id not in self.agent_states
        ]
        if not roots:
            return Text(self.translator.text("flow_idle"), style="dim")

        rendered = Text()
        ordered_roots = sorted(roots, key=self._tree_sort_key)
        for index, state in enumerate(ordered_roots):
            self._append_overview_state_lines(rendered, state, depth=0)
            if index < len(ordered_roots) - 1:
                rendered.append("\n")
        return rendered

    def _append_overview_state_lines(self, target: Text, state: AgentRuntimeView, *, depth: int) -> None:
        indent = "  " * depth
        target.append(f"{indent}{state.name} ", style="bold")
        target.append(f"({state.id}) ", style="dim")
        target.append(self._status_label(state.status), style=self._status_style(state.status))
        if state.step_count:
            target.append(f" | {self.translator.text('step_label')} {state.step_count}", style="dim")
        target.append(
            f" | {self.translator.text('output_tokens_short')} {max(0, int(state.output_tokens_total))}",
            style="dim",
        )
        if state.context_limit_tokens > 0:
            target.append(
                (
                    " | "
                    f"ctx {max(0, int(state.current_context_tokens))}/{max(0, int(state.context_limit_tokens))}"
                ),
                style="dim",
            )
        if state.compression_count > 0:
            target.append(
                f" | {self.translator.text('compression_count_label')} {max(0, int(state.compression_count))}",
                style="dim",
            )
        if state.model:
            target.append(f" | {self.translator.text('agent_model_label')} {state.model}", style="dim")
        if state.role == "root":
            target.append(f" | {self.translator.text('root_value')}", style="dim")
        target.append("\n")

        detail = state.summary or state.last_detail or state.instruction or self.translator.text("none_value")
        detail_lines = detail.splitlines() or [detail]
        for line in detail_lines:
            target.append(f"{indent}  {line}\n", style="white")

        for child in self._sorted_agent_children(state.id):
            self._append_overview_state_lines(target, child, depth=depth + 1)

    def _render_live_panel_content(self) -> Text:
        visible_ids = self._visible_live_agent_ids()
        if not visible_ids:
            return Text(self.translator.text("no_active_stream"), style="dim")

        child_map = self._agent_children_map()
        visible_set = set(visible_ids)
        roots = [
            self.agent_states[agent_id]
            for agent_id in visible_ids
            if (
                self.agent_states[agent_id].parent_agent_id is None
                or self.agent_states[agent_id].parent_agent_id not in visible_set
            )
        ]

        rendered = Text()
        for index, state in enumerate(roots):
            self._append_live_state_lines(rendered, state, child_map, visible_set, depth=0)
            if index < len(roots) - 1:
                rendered.append("\n")
        return rendered

    def _append_live_state_lines(
        self,
        target: Text,
        state: AgentRuntimeView,
        child_map: dict[str | None, list[AgentRuntimeView]],
        visible_set: set[str],
        *,
        depth: int,
    ) -> None:
        indent = "  " * depth
        target.append(f"{indent}{state.name} ", style="bold")
        target.append(f"({state.id}) ", style="dim")
        target.append(self._status_label(state.status), style=self._status_style(state.status))
        meta_bits: list[str] = []
        if state.step_count:
            meta_bits.append(f"{self.translator.text('step_label')} {state.step_count}")
        meta_bits.append(
            f"{self.translator.text('output_tokens_short')} {max(0, int(state.output_tokens_total))}"
        )
        if state.context_limit_tokens > 0:
            meta_bits.append(
                f"ctx {max(0, int(state.current_context_tokens))}/{max(0, int(state.context_limit_tokens))}"
            )
        if state.compression_count > 0:
            meta_bits.append(
                f"{self.translator.text('compression_count_label')} {max(0, int(state.compression_count))}"
            )
        if state.model:
            meta_bits.append(f"{self.translator.text('agent_model_label')} {state.model}")
        if state.last_phase:
            meta_bits.append(state.last_phase)
        if state.is_generating:
            meta_bits.append(self.translator.text("generating"))
        if meta_bits:
            target.append(f" | {' | '.join(meta_bits)}", style="dim")
        target.append("\n")

        entries = self._entries_for_stream_render(state)
        for kind, body in entries:
            body_lines = body.splitlines() or [body]
            target.append(f"{indent}  {self._stream_label(kind)}: ", style=self._stream_label_style(kind))
            target.append((body_lines[0] or " ") + "\n", style=self._stream_body_style(kind))
            for line in body_lines[1:]:
                target.append(f"{indent}    {line}\n", style=self._stream_body_style(kind))

        for child in child_map.get(state.id, []):
            if child.id in visible_set:
                self._append_live_state_lines(target, child, child_map, visible_set, depth=depth + 1)

    def _entries_for_stream_render(self, state: AgentRuntimeView) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for step_number in self._effective_step_order(state):
            entries.extend(self._entries_for_step(state, step_number))
        if not entries:
            entries = [
                (kind, body)
                for kind, body in state.stream_entries
                if self._should_display_live_stream_kind(kind)
            ]
        if state.summary and ("summary", state.summary) not in entries:
            entries.append(("summary", state.summary))
        return entries

    def _config_project_dir_text(self) -> str:
        if self.remote_config is not None:
            return shorten(
                f"{self.remote_config.ssh_target}:{self.remote_config.remote_dir}",
                width=56,
                placeholder="...",
            )
        if self.project_dir is None:
            return self.translator.text("unset_value")
        if self.project_dir.is_dir():
            return shorten(str(self.project_dir), width=56, placeholder="...")
        return shorten(
            f"{self.project_dir} ({self.translator.text('invalid_value')})",
            width=56,
            placeholder="...",
        )

    def _project_sync_status_text(self) -> str:
        status = self._project_sync_status()
        if status == "disabled":
            return self.translator.text("sync_state_disabled")
        if status == "pending":
            return self.translator.text("sync_state_pending")
        if status == "applied":
            return self.translator.text("sync_state_applied")
        if status == "reverted":
            return self.translator.text("sync_state_reverted")
        if status == "none":
            return self.translator.text("sync_state_none")
        if status == "error":
            return self.translator.text("sync_state_error")
        return status

    def _config_actions_text(self, config: SessionLaunchConfig) -> str:
        actions: list[str] = []
        if config.can_run():
            actions.append(self.translator.text("run"))
        if self._can_apply_project_sync():
            actions.append(self.translator.text("apply"))
        if self._can_undo_project_sync():
            actions.append(self.translator.text("undo"))
        return ", ".join(actions) if actions else self.translator.text("none_value")

    def _config_status_label(self, config: SessionLaunchConfig) -> str:
        return (
            self.translator.text("configuration_ready")
            if config.can_run()
            else self.translator.text("configuration_missing")
        )

    def _display_session_id(self, session_id: str | None, *, empty_key: str) -> str:
        if not session_id:
            return self.translator.text(empty_key)
        return session_id if len(session_id) <= 12 else session_id[:8]

    @staticmethod
    def _is_preview_stream_kind(kind: str) -> bool:
        return str(kind).endswith("_preview")

    @classmethod
    def _should_display_live_stream_kind(cls, kind: str) -> bool:
        return not cls._is_non_message_stream_kind(kind)

    @classmethod
    def _is_non_message_stream_kind(cls, kind: str) -> bool:
        text = str(kind)
        return text.endswith("_extra") or cls._is_preview_stream_kind(text)

    @classmethod
    def _base_stream_kind(cls, kind: str) -> str:
        text = str(kind)
        if text.endswith("_extra"):
            return text[: -len("_extra")]
        if cls._is_preview_stream_kind(text):
            return text[: -len("_preview")]
        return text

    def _stream_label(self, kind: str) -> str:
        base_kind = self._base_stream_kind(kind)
        mapping = {
            "thinking": self.translator.text("stream_thinking"),
            "reply": self.translator.text("stream_reply"),
            "response": self.translator.text("stream_reply"),
            "tool": self.translator.text("stream_tool"),
            "tool_call": self.translator.text("stream_tool_call"),
            "tool_return": self.translator.text("stream_tool_return"),
            "user_message": self.translator.text("message"),
            "tool_message": self.translator.text("stream_tool_return"),
            "multiagent": self.translator.text("stream_multiagent"),
            "multiagent_call": self.translator.text("stream_multiagent_call"),
            "multiagent_return": self.translator.text("stream_multiagent_return"),
            "stdout": self.translator.text("stream_stdout"),
            "stderr": self.translator.text("stream_stderr"),
            "summary": self.translator.text("stream_summary"),
            "error": self.translator.text("stream_error"),
            "generating": self.translator.text("stream_generating"),
            "control": self.translator.text("stream_control"),
        }
        label = mapping.get(base_kind, base_kind)
        if self._is_non_message_stream_kind(kind) or base_kind in {"stdout", "stderr"}:
            label = f"{label} ({self.translator.text('stream_not_in_messages')})"
        icons = {
            "thinking": "💭",
            "reply": "💬",
            "response": "🗨️",
            "tool": "🛠️",
            "tool_call": "🔧",
            "tool_return": "🧰",
            "user_message": "👤",
            "tool_message": "🧾",
            "multiagent": "🕸️",
            "multiagent_call": "🤝",
            "multiagent_return": "📬",
            "stdout": "📤",
            "stderr": "⚠️",
            "summary": "📝",
            "error": "❌",
            "generating": "⏳",
            "control": "🧭",
        }
        icon = icons.get(base_kind, "")
        return f"{icon} {label}" if icon else label

    def _remember_collapsible_toggle(self, collapsible: Collapsible, *, expanded: bool) -> None:
        if isinstance(collapsible, AgentCollapsible):
            target = (
                self.overview_collapsed_agent_ids
                if collapsible.panel_kind == "overview"
                else self.live_collapsed_agent_ids
            )
            if expanded:
                target.discard(collapsible.agent_id)
            else:
                target.add(collapsible.agent_id)
            return
        if isinstance(collapsible, AgentSectionCollapsible):
            if collapsible.section_kind == "instruction":
                if expanded:
                    self.overview_instruction_collapsed_agent_ids.discard(collapsible.agent_id)
                else:
                    self.overview_instruction_collapsed_agent_ids.add(collapsible.agent_id)
                return
            if collapsible.section_kind == "summary":
                if expanded:
                    self.overview_summary_expanded_agent_ids.add(collapsible.agent_id)
                else:
                    self.overview_summary_expanded_agent_ids.discard(collapsible.agent_id)
                return
        if isinstance(collapsible, LiveStepCollapsible):
            self.live_step_collapsed_overrides[(collapsible.agent_id, collapsible.step_number)] = not expanded

    def _build_overview_widgets(self) -> list[Widget]:
        roots = [
            state
            for state in self.agent_states.values()
            if not state.parent_agent_id or state.parent_agent_id not in self.agent_states
        ]
        if not roots:
            return [Static(self.translator.text("flow_idle"), classes="empty-state")]
        order_index = {agent_id: index for index, agent_id in enumerate(self.stream_agent_order)}
        roots.sort(key=lambda state: order_index.get(state.id, len(order_index)))
        return [self._build_overview_agent_widget(state) for state in roots]

    def _build_live_widgets(self) -> list[Widget]:
        visible_ids = self._visible_live_agent_ids()
        if not visible_ids:
            return [Static(self.translator.text("no_active_stream"), classes="empty-state")]

        return [
            self._build_live_agent_widget(state)
            for agent_id in visible_ids
            for state in [self.agent_states.get(agent_id)]
            if state is not None
        ]

    def _build_overview_agent_widget(self, state: AgentRuntimeView) -> AgentCollapsible:
        agent_key = self._widget_safe_id(state.id)
        instruction_body = state.instruction or self.translator.text("none_value")
        summary_body = self._overview_summary_text(state)
        children: list[Widget] = [
            AgentSectionCollapsible(
                Static(
                    self._section_body_text(instruction_body, style="yellow"),
                    classes="agent-detail",
                    id=f"wf-agent-{agent_key}-instruction-body",
                ),
                agent_id=state.id,
                section_kind="instruction",
                title=f"[bold yellow]{escape(self.translator.text('task'))}[/]",
                collapsed=self._is_overview_instruction_collapsed(state.id),
                id=f"wf-agent-{agent_key}-instruction",
            ),
            AgentSectionCollapsible(
                Static(
                    self._section_body_text(summary_body, style="cyan"),
                    classes="agent-detail",
                    id=f"wf-agent-{agent_key}-summary-body",
                ),
                agent_id=state.id,
                section_kind="summary",
                title=f"[bold cyan]{escape(self.translator.text('summary'))}[/]",
                collapsed=self._is_overview_summary_collapsed(state.id),
                id=f"wf-agent-{agent_key}-summary",
            ),
        ]
        for child in self._sorted_agent_children(state.id):
            children.append(self._build_overview_agent_widget(child))
        return AgentCollapsible(
            *children,
            agent_id=state.id,
            panel_kind="overview",
            title=self._overview_agent_title(state),
            collapsed=state.id in self.overview_collapsed_agent_ids,
            id=f"wf-agent-{agent_key}",
        )

    def _build_live_agent_widget(self, state: AgentRuntimeView) -> AgentCollapsible:
        agent_key = self._widget_safe_id(state.id)
        children: list[Widget] = [
            Static(
                self._live_agent_status_body(state),
                classes="agent-detail",
                id=f"live-agent-{agent_key}-status-body",
            )
        ]
        children.append(
            Horizontal(
                AgentCopyButton(
                    self.translator.text("copy_agent_name_button"),
                    copy_value=state.name,
                    copy_kind="name",
                    id=f"live-agent-{agent_key}-copy-name-button",
                ),
                AgentCopyButton(
                    self.translator.text("copy_agent_id_button"),
                    copy_value=state.id,
                    copy_kind="id",
                    id=f"live-agent-{agent_key}-copy-id-button",
                ),
                classes="agent-jump-row",
                id=f"live-agent-{agent_key}-copy-row",
            )
        )
        children.append(
            Horizontal(
                AgentSteerButton(
                    self.translator.text("steer_button"),
                    target_agent_id=state.id,
                    id=f"live-agent-{agent_key}-steer-button",
                ),
                AgentTerminateButton(
                    self.translator.text("terminate_button"),
                    target_agent_id=state.id,
                    id=f"live-agent-{agent_key}-terminate-button",
                ),
                classes="agent-jump-row",
                id=f"live-agent-{agent_key}-steer-row",
            )
        )
        jump_buttons = self._build_live_agent_jump_buttons(state, agent_key)
        if jump_buttons:
            children.append(
                Horizontal(
                    *jump_buttons,
                    classes="agent-jump-row",
                    id=f"live-agent-{agent_key}-jump-row",
                )
            )
        step_order = self._effective_step_order(state)
        latest_step = step_order[-1] if step_order else 0
        for step_number in step_order:
            collapsed = self._is_live_step_collapsed(state.id, step_number, latest_step)
            children.append(
                LiveStepCollapsible(
                    Vertical(
                        *self._build_live_step_body_widgets(state, step_number, agent_key),
                        classes="live-step-body",
                        id=f"live-agent-{agent_key}-step-{step_number}-body",
                    ),
                    agent_id=state.id,
                    step_number=step_number,
                    title=self._live_step_title(state, step_number),
                    collapsed=collapsed,
                    id=f"live-agent-{agent_key}-step-{step_number}",
                )
            )
        return AgentCollapsible(
            *children,
            agent_id=state.id,
            panel_kind="live",
            title=self._live_agent_title(state),
            collapsed=state.id in self.live_collapsed_agent_ids,
            id=f"live-agent-{agent_key}",
        )

    def _build_live_agent_jump_buttons(self, state: AgentRuntimeView, agent_key: str) -> list[AgentJumpButton]:
        buttons: list[AgentJumpButton] = []
        parent_id = (state.parent_agent_id or "").strip()
        parent = self.agent_states.get(parent_id) if parent_id else None
        if parent is not None:
            buttons.append(
                AgentJumpButton(
                    f"{self.translator.text('parent_agent_label')}: {parent.name}",
                    target_agent_id=parent.id,
                    id=f"live-agent-{agent_key}-jump-parent",
                )
            )
        for index, child in enumerate(self._sorted_agent_children(state.id)):
            buttons.append(
                AgentJumpButton(
                    f"{self.translator.text('child_agents_label')}: {child.name}",
                    target_agent_id=child.id,
                    id=f"live-agent-{agent_key}-jump-child-{index}",
                )
            )
        return buttons

    async def _sync_live_agent_jump_buttons(
        self,
        container: Horizontal,
        state: AgentRuntimeView,
        agent_key: str,
    ) -> None:
        desired_buttons = self._build_live_agent_jump_buttons(state, agent_key)
        desired_ids = [button.id or "" for button in desired_buttons]
        existing_ids = [child.id or "" for child in container.children]
        if existing_ids != desired_ids:
            await self._replace_panel_children(container, desired_buttons)
            return
        for button in desired_buttons:
            if button.id is None:
                continue
            current = self._query_optional(f"#{button.id}", AgentJumpButton)
            if current is None:
                continue
            current.label = button.label
            current.target_agent_id = button.target_agent_id

    def _overview_agent_title(self, state: AgentRuntimeView) -> str:
        status = self._status_label(state.status)
        meta: list[str] = []
        if state.step_count:
            meta.append(f"{self.translator.text('step_label')} {state.step_count}")
        meta.append(f"{self.translator.text('output_tokens_short')} {max(0, int(state.output_tokens_total))}")
        if state.compression_count > 0:
            meta.append(
                f"{self.translator.text('compression_count_label')} {max(0, int(state.compression_count))}"
            )
        if state.role == "root":
            meta.append(self.translator.text("root_value"))
        if state.model:
            meta.append(f"{self.translator.text('agent_model_label')}={state.model}")
        meta_text = f" | {' | '.join(meta)}" if meta else ""
        return (
            f"[bold]{escape(state.name)}[/] [dim]({escape(state.id)})[/] "
            f"[{self._status_style(state.status)}]{escape(status)}[/]{escape(meta_text)}"
        )

    def _live_agent_title(self, state: AgentRuntimeView) -> str:
        status = self._status_label(state.status)
        meta: list[str] = []
        if state.last_phase:
            meta.append(state.last_phase)
        if state.model:
            meta.append(f"{self.translator.text('agent_model_label')}={state.model}")
        if state.compression_count > 0:
            meta.append(
                f"{self.translator.text('compression_count_label')}={max(0, int(state.compression_count))}"
            )
        if state.is_generating:
            meta.append(self.translator.text("generating"))
        meta_text = f" | {' | '.join(meta)}" if meta else ""
        return (
            f"[bold]{escape(state.name)}[/] [dim]({escape(state.id)})[/] "
            f"[{self._status_style(state.status)}]{escape(status)}[/]{escape(meta_text)}"
        )

    def _overview_agent_body(self, state: AgentRuntimeView) -> Text:
        text = Text()
        self._append_section(
            text,
            self.translator.text("task"),
            state.instruction or self.translator.text("none_value"),
            label_style="bold yellow",
            body_style="yellow",
        )
        if state.summary:
            self._append_section(
                text,
                self.translator.text("summary"),
                state.summary,
                label_style="bold cyan",
                body_style="cyan",
            )
        elif state.last_detail or state.last_event:
            self._append_section(
                text,
                self.translator.text("message"),
                state.last_detail or state.last_event,
                label_style="bold white",
                body_style="white",
            )
        return text

    def _live_agent_status_body(self, state: AgentRuntimeView) -> Text:
        text = Text()
        status_bits = [self._status_label(state.status)]
        if state.step_count:
            status_bits.append(f"{self.translator.text('step_label')} {state.step_count}")
        if state.last_phase:
            status_bits.append(state.last_phase)
        if state.last_detail:
            status_bits.append(state.last_detail)
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("status"),
                    " | ".join(bit for bit in status_bits if bit),
                    self._status_style(state.status),
                ),
            ),
        )
        context_value = self.translator.text("none_value")
        if state.context_limit_tokens > 0:
            context_value = (
                f"{max(0, int(state.current_context_tokens))}/"
                f"{max(0, int(state.context_limit_tokens))}"
            )
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("output_tokens_total"),
                    str(max(0, int(state.output_tokens_total))),
                    "green",
                ),
                (
                    self.translator.text("context_tokens_total"),
                    context_value,
                    "green",
                ),
                (
                    self.translator.text("usage_ratio_label"),
                    f"{max(0.0, float(state.usage_ratio)):.4f}",
                    "green",
                ),
            ),
        )
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("usage_last_total_label"),
                    self._usage_total_text(state),
                    "green",
                ),
                (
                    self.translator.text("usage_last_cache_label"),
                    self._usage_cache_text(state),
                    "green",
                ),
            ),
        )
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("compression_count_label"),
                    str(max(0, int(state.compression_count))),
                    "green",
                ),
                (
                    self.translator.text("last_compacted_label"),
                    state.last_compacted_step_range or self.translator.text("none_value"),
                    "white",
                ),
                (
                    self.translator.text("keep_pinned_messages_label"),
                    str(max(0, int(state.keep_pinned_messages))),
                    "green",
                ),
                (
                    self.translator.text("summary_version_label"),
                    self._summary_version_text(state),
                    "green",
                ),
            ),
        )
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("agent_model_label"),
                    state.model or self.translator.text("none_value"),
                    "cyan",
                ),
            ),
        )
        self._append_compact_metric_line(
            text,
            (
                (
                    self.translator.text("parent_agent_label"),
                    self._live_parent_agent_text(state),
                    "white",
                ),
                (
                    self.translator.text("child_agents_label"),
                    self._live_child_agents_text(state),
                    "white",
                ),
            ),
        )
        return text

    def _append_compact_metric_line(
        self,
        target: Text,
        segments: tuple[tuple[str, str, str], ...],
    ) -> None:
        normalized: list[tuple[str, str, str]] = []
        for label, value, style in segments:
            normalized_label = str(label or "").strip()
            if not normalized_label:
                continue
            normalized_value = str(value or "").strip() or self.translator.text("none_value")
            normalized.append((normalized_label, normalized_value, style))
        if not normalized:
            return
        if target:
            target.append("\n")
        for index, (label, value, style) in enumerate(normalized):
            if index > 0:
                target.append("  |  ", style="dim")
            target.append(f"{label}: ", style="bold white")
            target.append(value, style=style)

    def _usage_token_or_dash(self, value: int | None) -> str:
        if value is None:
            return "-"
        return str(max(0, int(value)))

    def _usage_cache_text(self, state: AgentRuntimeView) -> str:
        if (
            state.last_usage_cache_read_tokens is None
            and state.last_usage_cache_write_tokens is None
        ):
            return self.translator.text("none_value")
        return (
            f"{self.translator.text('token_cache_read_short')} "
            f"{self._usage_token_or_dash(state.last_usage_cache_read_tokens)} / "
            f"{self.translator.text('token_cache_write_short')} "
            f"{self._usage_token_or_dash(state.last_usage_cache_write_tokens)}"
        )

    def _usage_total_text(self, state: AgentRuntimeView) -> str:
        total_tokens = state.last_usage_total_tokens
        input_tokens = state.last_usage_input_tokens
        output_tokens = state.last_usage_output_tokens
        if total_tokens is None and input_tokens is None and output_tokens is None:
            return self.translator.text("none_value")
        resolved_total = (
            max(0, int(total_tokens))
            if total_tokens is not None
            else max(0, int(input_tokens or 0) + int(output_tokens or 0))
        )
        if input_tokens is None and output_tokens is None:
            return str(resolved_total)
        return (
            f"{resolved_total} "
            f"({self.translator.text('token_input_short')} "
            f"{self._usage_token_or_dash(input_tokens)} + "
            f"{self.translator.text('token_output_short')} "
            f"{self._usage_token_or_dash(output_tokens)})"
        )

    def _summary_version_text(self, state: AgentRuntimeView) -> str:
        version = max(0, int(state.summary_version))
        if version <= 0:
            return self.translator.text("none_value")
        return f"v{version}"

    def _context_latest_summary_text(
        self,
        state: AgentRuntimeView,
    ) -> str:
        summary = str(state.context_latest_summary or "").strip()
        if not summary:
            return self.translator.text("none_value")
        return summary

    def _agent_reference_text(self, state: AgentRuntimeView) -> str:
        return f"{state.name} ({state.id})"

    def _live_parent_agent_text(self, state: AgentRuntimeView) -> str:
        parent_id = (state.parent_agent_id or "").strip()
        if not parent_id:
            return self.translator.text("none_value")
        parent = self.agent_states.get(parent_id)
        if parent is None:
            return parent_id
        return self._agent_reference_text(parent)

    def _live_child_agents_text(self, state: AgentRuntimeView) -> str:
        children = self._sorted_agent_children(state.id)
        if not children:
            return self.translator.text("none_value")
        return ", ".join(self._agent_reference_text(child) for child in children)

    def _live_step_title(self, state: AgentRuntimeView, step_number: int) -> str:
        step_entries = self._entries_for_step(state, step_number)
        entry_count = len(step_entries)
        step_order = self._effective_step_order(state)
        latest_step = step_order[-1] if step_order else 0
        is_latest_generating = state.is_generating and step_number == latest_step
        if step_number == 0 and self._has_context_summary(state):
            step_label = self.translator.text("context_latest_summary_label")
        else:
            step_label = f"{self.translator.text('step_label')} {step_number}"
        meta_parts = [f"{entry_count} {self.translator.text('events_label')}"]
        if is_latest_generating:
            meta_parts.append(self.translator.text("generating"))
        meta = f" | {' | '.join(meta_parts)}"
        return (
            f"[bold]{escape(step_label)}[/]"
            f"{escape(meta)}"
        )

    def _overview_summary_text(self, state: AgentRuntimeView) -> str:
        if state.summary:
            return state.summary
        if state.status in {"failed", "terminated"} and (state.last_detail or state.last_event):
            return state.last_detail or state.last_event
        return self.translator.text("none_value")

    def _build_live_step_body_widgets(
        self,
        state: AgentRuntimeView,
        step_number: int,
        agent_key: str,
    ) -> list[Widget]:
        entries = self._entries_for_step(state, step_number)
        if not entries:
            return [
                Static(
                    self._live_step_empty_body(),
                    classes="agent-detail",
                    id=f"live-agent-{agent_key}-step-{step_number}-entry-empty",
                )
            ]
        widgets: list[Widget] = []
        for index, (kind, body) in enumerate(entries):
            kind_key = self._widget_safe_id(kind or "entry")
            widgets.append(
                Static(
                    self._live_step_entry_body(
                        kind,
                        body,
                        show_label=self._show_stream_label_for_entry(entries, index),
                    ),
                    classes=self._live_step_entry_classes(kind),
                    id=f"live-agent-{agent_key}-step-{step_number}-entry-{index}-{kind_key}",
                )
            )
        return widgets

    async def _sync_live_step_body_widgets(
        self,
        container: Vertical,
        state: AgentRuntimeView,
        step_number: int,
        agent_key: str,
    ) -> None:
        entries = self._entries_for_step(state, step_number)
        existing_children = list(container.children)
        self._prune_live_step_entry_fingerprints(state.id, step_number, len(entries))

        if not entries:
            empty_id = f"live-agent-{agent_key}-step-{step_number}-entry-empty"
            empty_key = (state.id, step_number, -1)
            empty_fingerprint = self._empty_step_entry_render_fingerprint()
            if len(existing_children) != 1 or existing_children[0].id != empty_id:
                await self._replace_panel_children(
                    container,
                    [Static(self._live_step_empty_body(), classes="agent-detail", id=empty_id)],
                )
                self._live_step_entry_render_fingerprints[empty_key] = empty_fingerprint
                return
            empty_widget = self._query_optional(f"#{empty_id}", Static)
            if empty_widget is not None and (
                self._live_step_entry_render_fingerprints.get(empty_key) != empty_fingerprint
            ):
                empty_widget.update(self._live_step_empty_body())
                self._live_step_entry_render_fingerprints[empty_key] = empty_fingerprint
            return

        desired_ids = [
            (
                f"live-agent-{agent_key}-step-{step_number}-entry-{index}-"
                f"{self._widget_safe_id((entries[index][0] or 'entry'))}"
            )
            for index in range(len(entries))
        ]
        existing_ids = [child.id or "" for child in existing_children]
        if existing_ids != desired_ids:
            await self._replace_panel_children(
                container,
                self._build_live_step_body_widgets(state, step_number, agent_key),
            )
            for index in range(len(entries)):
                self._live_step_entry_render_fingerprints[(state.id, step_number, index)] = (
                    self._live_step_entry_render_fingerprint(entries, index)
                )
            return

        for index, (kind, body) in enumerate(entries):
            fingerprint = self._live_step_entry_render_fingerprint(entries, index)
            key = (state.id, step_number, index)
            if self._live_step_entry_render_fingerprints.get(key) == fingerprint:
                continue
            entry_widget = self._query_optional(f"#{desired_ids[index]}", Static)
            if entry_widget is not None:
                entry_widget.update(
                    self._live_step_entry_body(
                        kind,
                        body,
                        show_label=self._show_stream_label_for_entry(entries, index),
                    )
                )
                self._live_step_entry_render_fingerprints[key] = fingerprint

    def _live_step_entry_render_fingerprint(
        self,
        entries: list[tuple[str, str]],
        index: int,
    ) -> tuple[Any, ...]:
        kind, body = entries[index]
        return (kind, body, self._show_stream_label_for_entry(entries, index))

    def _empty_step_entry_render_fingerprint(self) -> tuple[Any, ...]:
        return (self.translator.text("no_active_stream"),)

    @staticmethod
    def _live_step_entry_classes(kind: str) -> str:
        if OpenCompanyApp._is_non_message_stream_kind(kind):
            return "agent-detail non-message-stream"
        return "agent-detail"

    def _prune_live_step_entry_fingerprints(
        self,
        agent_id: str,
        step_number: int,
        entry_count: int,
    ) -> None:
        for key in list(self._live_step_entry_render_fingerprints.keys()):
            if key[0] != agent_id or key[1] != step_number:
                continue
            index = key[2]
            if entry_count <= 0:
                if index != -1:
                    self._live_step_entry_render_fingerprints.pop(key, None)
                continue
            if index < 0 or index >= entry_count:
                self._live_step_entry_render_fingerprints.pop(key, None)

    def _live_step_empty_body(self) -> Text:
        text = Text()
        self._append_section(
            text,
            self.translator.text("message"),
            self.translator.text("no_active_stream"),
            label_style="bold white",
            body_style="dim",
        )
        return text

    @staticmethod
    def _stream_label_group(kind: str) -> str:
        base_kind = OpenCompanyApp._base_stream_kind(kind)
        return "reply" if base_kind == "response" else base_kind

    def _show_stream_label_for_entry(
        self,
        entries: list[tuple[str, str]],
        index: int,
    ) -> bool:
        if index <= 0:
            return True
        kind, body = entries[index]
        previous_kind, previous_body = entries[index - 1]
        if body != previous_body:
            return True
        return self._stream_label_group(kind) != self._stream_label_group(previous_kind)

    def _live_step_entry_body(self, kind: str, body: str, *, show_label: bool = True) -> Text:
        text = Text()
        body_style = self._stream_body_style(kind)
        if show_label:
            self._append_section(
                text,
                self._stream_label(kind),
                body,
                label_style=self._stream_label_style(kind),
                body_style=body_style,
            )
        else:
            text.append_text(self._section_body_text(body, style=body_style))
        return text

    def _live_step_body(self, state: AgentRuntimeView, step_number: int) -> Text:
        text = Text()
        entries = self._entries_for_step(state, step_number)
        if not entries:
            return self._live_step_empty_body()
        for index, (kind, body) in enumerate(entries):
            entry = self._live_step_entry_body(
                kind,
                body,
                show_label=self._show_stream_label_for_entry(entries, index),
            )
            if text:
                text.append("\n\n")
            text.append_text(entry)
        return text

    def _effective_step_order(self, state: AgentRuntimeView) -> list[int]:
        real_steps = self._real_step_order(state)
        if not self._has_context_summary(state):
            return real_steps
        pinned_steps = self._pinned_head_steps(state, real_steps)
        pinned_set = set(pinned_steps)
        summarized_steps = self._summarized_step_numbers(state)
        unsummarized_steps = [
            step_number
            for step_number in real_steps
            if step_number not in pinned_set and step_number not in summarized_steps
        ]
        return [*pinned_steps, 0, *unsummarized_steps]

    def _has_context_summary(self, state: AgentRuntimeView) -> bool:
        if str(state.context_latest_summary or "").strip():
            return True
        return max(0, int(state.summary_version)) > 0

    def _real_step_order(self, state: AgentRuntimeView) -> list[int]:
        candidates: list[int] = []
        for raw_step in state.step_order:
            try:
                step_number = int(raw_step)
            except (TypeError, ValueError):
                continue
            if step_number > 0:
                candidates.append(step_number)
        for raw_step in state.step_entries.keys():
            try:
                step_number = int(raw_step)
            except (TypeError, ValueError):
                continue
            if step_number > 0:
                candidates.append(step_number)
        if not candidates and state.stream_entries:
            candidates.append(max(state.step_count, 1))
        return sorted(set(candidates))

    def _pinned_head_steps(
        self,
        state: AgentRuntimeView,
        real_steps: list[int] | None = None,
    ) -> list[int]:
        ordered = real_steps if real_steps is not None else self._real_step_order(state)
        if not ordered:
            return []
        keep_count = max(0, int(state.keep_pinned_messages))
        if keep_count <= 0:
            return []
        return ordered[: min(keep_count, len(ordered))]

    def _summarized_step_numbers(self, state: AgentRuntimeView) -> set[int]:
        summarized: set[int] = set()
        for start_step, end_step in sorted(state.compacted_step_ranges):
            if start_step <= 0 or end_step < start_step:
                continue
            for step_number in range(start_step, end_step + 1):
                summarized.add(step_number)
        return summarized

    def _entries_for_step(self, state: AgentRuntimeView, step_number: int) -> list[tuple[str, str]]:
        def _visible(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
            return [
                (kind, body)
                for kind, body in entries
                if self._should_display_live_stream_kind(kind)
            ]

        if step_number == 0 and self._has_context_summary(state):
            return [("summary", self._context_latest_summary_text(state))]
        if step_number <= 0:
            return []
        if step_number in state.step_entries:
            step_entries = _visible(list(state.step_entries.get(step_number, [])))
        else:
            fallback_step = max(state.step_count, 1)
            if step_number == fallback_step:
                step_entries = _visible(list(state.stream_entries))
            else:
                step_entries = []
        if not self._has_context_summary(state):
            return step_entries
        pinned_set = set(self._pinned_head_steps(state))
        if step_number in pinned_set:
            return step_entries
        summarized_steps = self._summarized_step_numbers(state)
        if step_number in summarized_steps:
            return []
        return step_entries

    def _is_live_step_collapsed(self, agent_id: str, step_number: int, latest_step: int) -> bool:
        del latest_step
        return self.live_step_collapsed_overrides.get((agent_id, step_number), True)

    def _live_agent_body(self, state: AgentRuntimeView) -> Text:
        text = Text()
        status_bits = [self._status_label(state.status)]
        if state.step_count:
            status_bits.append(f"{self.translator.text('step_label')} {state.step_count}")
        if state.last_phase:
            status_bits.append(state.last_phase)
        if state.last_detail:
            status_bits.append(state.last_detail)
        self._append_section(
            text,
            self.translator.text("status"),
            " | ".join(bit for bit in status_bits if bit),
            label_style="bold white",
            body_style=self._status_style(state.status),
        )
        entries = self._entries_for_stream_render(state)
        if not entries:
            self._append_section(
                text,
                self.translator.text("message"),
                self.translator.text("no_active_stream"),
                label_style="bold white",
                body_style="dim",
            )
            return text
        for index, (kind, body) in enumerate(entries):
            show_label = self._show_stream_label_for_entry(entries, index)
            if show_label:
                self._append_section(
                    text,
                    self._stream_label(kind),
                    body,
                    label_style=self._stream_label_style(kind),
                    body_style=self._stream_body_style(kind),
                )
                continue
            if text:
                text.append("\n\n")
            text.append_text(self._section_body_text(body, style=self._stream_body_style(kind)))
        return text

    def _append_section(
        self,
        target: Text,
        title: str,
        body: str,
        *,
        label_style: str,
        body_style: str,
    ) -> None:
        if target:
            target.append("\n\n")
        target.append(f"{title}\n", style=label_style)
        lines = body.splitlines() or [body]
        for index, line in enumerate(lines):
            target.append("  ", style=body_style)
            target.append(line if line else " ", style=body_style)
            if index < len(lines) - 1:
                target.append("\n")

    def _section_body_text(self, body: str, *, style: str) -> Text:
        text = Text()
        lines = body.splitlines() or [body]
        for index, line in enumerate(lines):
            text.append("  ", style=style)
            text.append(line if line else " ", style=style)
            if index < len(lines) - 1:
                text.append("\n")
        return text

    def _is_overview_instruction_collapsed(self, agent_id: str) -> bool:
        return agent_id in self.overview_instruction_collapsed_agent_ids

    def _is_overview_summary_collapsed(self, agent_id: str) -> bool:
        return agent_id not in self.overview_summary_expanded_agent_ids

    def _widget_safe_id(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", value)

    def _sorted_agent_children(self, parent_agent_id: str | None) -> list[AgentRuntimeView]:
        children = [
            candidate
            for candidate in self.agent_states.values()
            if candidate.parent_agent_id == parent_agent_id
        ]
        order_index = {agent_id: index for index, agent_id in enumerate(self.stream_agent_order)}
        children.sort(key=lambda state: order_index.get(state.id, len(order_index)))
        return children

    def _agent_children_map(self) -> dict[str | None, list[AgentRuntimeView]]:
        child_map: dict[str | None, list[AgentRuntimeView]] = {}
        order_index = {agent_id: index for index, agent_id in enumerate(self.stream_agent_order)}
        for state in self.agent_states.values():
            child_map.setdefault(state.parent_agent_id, []).append(state)
        for siblings in child_map.values():
            siblings.sort(key=lambda state: order_index.get(state.id, len(order_index)))
        return child_map

    def _visible_live_agent_ids(self) -> list[str]:
        ordered = [agent_id for agent_id in self.stream_agent_order if agent_id in self.agent_states]
        if len(ordered) == len(self.agent_states):
            return ordered
        known = set(ordered)
        for agent_id in self.agent_states:
            if agent_id not in known:
                ordered.append(agent_id)
        return ordered

    def _status_style(self, status: str) -> str:
        mapping = {
            "pending": "yellow",
            "running": "green",
            "paused": "blue",
            "completed": "cyan",
            "failed": "red",
            "cancelled": "magenta",
            "terminated": "red",
        }
        return mapping.get(status, "white")

    def _stream_label_style(self, kind: str) -> str:
        base_kind = self._base_stream_kind(kind)
        mapping = {
            "thinking": "bold blue",
            "reply": "bold green",
            "response": "bold green",
            "tool": "bold yellow",
            "tool_call": "bold yellow",
            "tool_return": "bold yellow",
            "user_message": "bold white",
            "tool_message": "bold yellow",
            "multiagent": "bold bright_cyan",
            "multiagent_call": "bold bright_cyan",
            "multiagent_return": "bold cyan",
            "stdout": "bold cyan",
            "stderr": "bold red",
            "summary": "bold blue",
            "error": "bold red",
            "generating": "bold white",
            "control": "bold magenta",
        }
        return mapping.get(base_kind, "bold white")

    def _stream_body_style(self, kind: str) -> str:
        base_kind = self._base_stream_kind(kind)
        mapping = {
            "thinking": "blue",
            "reply": "green",
            "response": "green",
            "tool": "yellow",
            "tool_call": "yellow",
            "tool_return": "yellow",
            "user_message": "white",
            "tool_message": "yellow",
            "multiagent": "bright_cyan",
            "multiagent_call": "bright_cyan",
            "multiagent_return": "cyan",
            "stdout": "white",
            "stderr": "red",
            "summary": "cyan",
            "error": "red",
            "generating": "dim",
            "control": "magenta",
        }
        return mapping.get(base_kind, "white")

    def _agent_counts(self) -> dict[str, int]:
        counts = {
            "running": 0,
            "paused": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "terminated": 0,
            "pending": 0,
        }
        for state in self.agent_states.values():
            if state.status in counts:
                counts[state.status] += 1
        return counts

    def _session_status_label(self, status: str) -> str:
        mapping = {
            "idle": self.translator.text("session_idle"),
            "starting": self.translator.text("session_starting"),
            "resuming": self.translator.text("session_resuming"),
            "running": self.translator.text("session_running"),
            "interrupting": self.translator.text("session_interrupting"),
            "completed": self.translator.text("session_completed"),
            "interrupted": self.translator.text("session_interrupted"),
            "failed": self.translator.text("session_failed"),
        }
        return mapping.get(status, status)

    def _status_label(self, status: str) -> str:
        mapping = {
            "pending": self.translator.text("status_pending"),
            "running": self.translator.text("status_running"),
            "paused": self.translator.text("status_paused"),
            "completed": self.translator.text("status_completed"),
            "failed": self.translator.text("status_failed"),
            "cancelled": self.translator.text("status_cancelled"),
            "terminated": self.translator.text("status_terminated"),
        }
        return mapping.get(status, status)

    def _set_controls_running(self, running: bool) -> None:
        config = self._launch_config()
        run_button = self._query_optional("#run_button", Button)
        model_input = self._query_optional("#model_input", Input)
        root_agent_name_input = self._query_optional("#root_agent_name_input", Input)
        terminal_button = self._query_optional("#terminal_button", Button)
        apply_button = self._query_optional("#apply_button", Button)
        undo_button = self._query_optional("#undo_button", Button)
        reconfigure_button = self._query_optional("#reconfigure_button", Button)
        interrupt_button = self._query_optional("#interrupt_button", Button)
        controls_busy = bool(self.project_sync_action_in_progress)
        if run_button is not None:
            run_button.disabled = controls_busy or not config.can_run()
        if model_input is not None:
            model_input.disabled = controls_busy
        if root_agent_name_input is not None:
            root_agent_name_input.disabled = controls_busy
        if terminal_button is not None:
            terminal_button.disabled = controls_busy or not bool(self._active_session_id())
        if apply_button is not None:
            apply_button.disabled = running or controls_busy or not self._can_apply_project_sync()
        if undo_button is not None:
            undo_button.disabled = running or controls_busy or not self._can_undo_project_sync()
        if reconfigure_button is not None:
            reconfigure_button.disabled = running or controls_busy
        if interrupt_button is not None:
            interrupt_button.disabled = not running
        self._update_diff_tab_availability()
        self._update_config_controls()

    def _update_diff_tab_availability(self) -> None:
        main_tabs = self._query_optional("#main_tabs", TabbedContent)
        if main_tabs is None:
            return
        try:
            diff_tab = main_tabs.get_tab("diff_tab")
        except Exception:
            return
        disabled = self._is_direct_workspace_mode()
        diff_tab.disabled = disabled
        if disabled and getattr(main_tabs, "active", None) == "diff_tab":
            main_tabs.active = "monitor_tab"

    def _clear_activity_log(self) -> None:
        log = self._query_optional("#activity_log", RichLog)
        if log is not None and hasattr(log, "clear"):
            log.clear()

    def _open_terminal(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            self._update_status(self.translator.text("error_session_required"))
            return
        try:
            orchestrator = self._terminal_orchestrator()
            if str(self.remote_password or "").strip():
                opened = orchestrator.open_session_terminal(
                    session_id,
                    remote_password=self.remote_password,
                )
            else:
                opened = orchestrator.open_session_terminal(session_id)
            self.app_dir = orchestrator.app_dir
        except Exception as exc:
            self._update_status(str(exc))
            return
        workspace_root = str(opened.get("workspace_root", "")).strip()
        if workspace_root:
            self._update_status(f"{self.translator.text('terminal_opened')}: {workspace_root}")
        else:
            self._update_status(self.translator.text("terminal_opened"))

    def _terminal_orchestrator(self) -> Orchestrator:
        if self.orchestrator is not None:
            return self.orchestrator
        orchestrator = self._create_orchestrator(
            self.project_dir or Path.cwd(),
            locale=self.locale,
            app_dir=self.app_dir,
        )
        self.orchestrator = orchestrator
        self.app_dir = orchestrator.app_dir
        return orchestrator

    def _open_launch_config(self) -> None:
        self._log_diagnostic("launch_config_opened")
        self.sandbox_backend_default = self._default_sandbox_backend_from_config()
        initial = self._launch_config()
        initial = SessionLaunchConfig.create(
            initial.project_dir,
            initial.session_id,
            session_mode=initial.session_mode,
            session_mode_locked=initial.session_mode_locked,
            sandbox_backend=self.sandbox_backend_default,
            remote=initial.remote,
            remote_password=initial.remote_password,
        )
        self.push_screen(
            SessionConfigScreen(
                translator=self.translator,
                initial_config=initial,
                sessions_dir=self._sessions_root_dir(),
                sandbox_backends=self._sandbox_backends,
                sandbox_backend_default=self.sandbox_backend_default,
            ),
            self._apply_launch_config,
        )

    def _sessions_root_dir(self) -> Path:
        app_dir = self._resolved_app_dir()
        config = OpenCompanyConfig.load(app_dir)
        return RuntimePaths.create(app_dir, config).sessions_dir

    def _apply_launch_config(self, config: SessionLaunchConfig | None) -> None:
        if config is None:
            if not self._launch_config().can_run():
                self._update_status(self.translator.text("configuration_required"))
            return
        self.remote_config = (
            normalize_remote_session_config(config.remote)
            if isinstance(config.remote, dict) and config.remote
            else None
        )
        self.remote_password = str(config.remote_password or "").strip()
        self.project_dir = None if self.remote_config is not None else config.project_dir
        self.configured_resume_session_id = config.session_id
        self.session_mode = normalize_workspace_mode(config.session_mode)
        self.session_mode_locked = bool(config.session_mode_locked)
        self.sandbox_backend = self._normalize_sandbox_backend(
            config.sandbox_backend,
            fallback=self.sandbox_backend_default,
        )
        self.orchestrator = None
        self.agent_states = {}
        self.stream_agent_order = []
        self.overview_collapsed_agent_ids = set()
        self.overview_instruction_collapsed_agent_ids = set()
        self.overview_summary_expanded_agent_ids = set()
        self.live_collapsed_agent_ids = set()
        self.live_step_collapsed_overrides = {}
        self.current_session_id = None
        self.current_task = ""
        self.current_session_status = "idle"
        self.current_focus_agent_id = None
        self.current_summary = ""
        self.project_sync_state = None
        self.last_project_sync_operation = None
        self._diff_preview_cache_key = None
        self._diff_preview_cache_data = None
        self._diff_render_cache_key = None
        self._overview_structure_signature = None
        self._live_structure_signature = None
        self._overview_render_fingerprints = {}
        self._live_agent_render_fingerprints = {}
        self._live_step_render_fingerprints = {}
        self._live_step_entry_render_fingerprints = {}
        self._status_panel_cache_text = ""
        self.tool_runs_snapshot = None
        self.tool_runs_metrics_snapshot = None
        self.tool_runs_status_message = ""
        self.tool_runs_filter = "all"
        self.tool_runs_group_by = "agent"
        self.tool_runs_selected_run_id = None
        self._tool_runs_dirty = True
        self._tool_runs_cache_key = None
        self._tool_run_call_id_to_run_id = {}
        self._tool_run_timeline_by_run_id = {}
        self._tool_runs_detail_open_run_id = None
        self._tool_runs_detail_run_snapshot = None
        self.steer_runs_snapshot = None
        self.steer_runs_metrics_snapshot = None
        self.steer_runs_status_message = ""
        self.steer_runs_filter = "all"
        self.steer_runs_group_by = "agent"
        self._steer_runs_dirty = True
        self._steer_runs_cache_key = None
        if isinstance(self.screen, ToolRunDetailScreen):
            self.screen.dismiss(None)
        self._set_task_input_text("", seeded_default=None)
        self._refresh_project_sync_state()
        self._clear_activity_log()
        self._update_status(
            (
                f"{self.translator.text('configuration_saved')} "
                f"({self.translator.text('sandbox_backend_label')}: {self._sandbox_backend_label()})"
            )
        )
        self._set_controls_running(False)
        self._render_all()
        if self.configured_resume_session_id:
            self._restore_session_history(self.configured_resume_session_id)
        self._log_diagnostic(
            "launch_config_saved",
            payload={
                "project_dir": str(self.project_dir) if self.project_dir is not None else None,
                "configured_session_id": self.configured_resume_session_id,
                "workspace_mode": self._workspace_mode().value,
                "sandbox_backend": self.sandbox_backend,
                "remote_target": self.remote_config.ssh_target if self.remote_config else None,
                "remote_dir": self.remote_config.remote_dir if self.remote_config else None,
            },
        )

    def _launch_config(self) -> SessionLaunchConfig:
        return SessionLaunchConfig.create(
            self.project_dir,
            self.configured_resume_session_id,
            session_mode=self.session_mode,
            session_mode_locked=self.session_mode_locked,
            sandbox_backend=self.sandbox_backend,
            remote=self._remote_config_payload(),
            remote_password=self.remote_password,
        )

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
        }
