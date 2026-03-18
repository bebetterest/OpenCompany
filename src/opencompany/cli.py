from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import getpass
import json
import os
from pathlib import Path
import re
import select
import time
from typing import Any, TextIO
import shutil
import sys
from textwrap import shorten
import unicodedata

try:
    import termios
    import tty
except Exception:  # pragma: no cover - non-POSIX environments
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from opencompany.config import OpenCompanyConfig
from opencompany.logging import DiagnosticLogger, diagnostics_path_for_app
from opencompany.models import RemoteSessionConfig, WorkspaceMode
from opencompany.orchestrator import Orchestrator, default_app_dir
from opencompany.paths import RuntimePaths
from opencompany.remote import (
    load_remote_session_config,
    load_remote_session_password,
    normalize_remote_session_config,
)
from opencompany.sandbox.registry import available_sandbox_backends, resolve_sandbox_backend_cls


def _cli_diagnostics(app_dir: Path | None) -> tuple[Path, DiagnosticLogger]:
    resolved_app_dir = (app_dir or default_app_dir()).resolve()
    return resolved_app_dir, DiagnosticLogger(diagnostics_path_for_app(resolved_app_dir))


def _available_sandbox_backend_names() -> tuple[str, ...]:
    backends = tuple(
        str(name).strip().lower()
        for name in available_sandbox_backends()
        if str(name).strip()
    )
    if backends:
        return backends
    return ("anthropic", "none")


def _normalize_sandbox_backend(
    backend: str | None,
    *,
    fallback: str | None = None,
) -> str:
    candidate = str(backend or "").strip().lower()
    available = _available_sandbox_backend_names()
    if candidate in available:
        return candidate
    fallback_candidate = str(fallback or "").strip().lower()
    if fallback_candidate in available:
        return fallback_candidate
    return available[0]


def _default_sandbox_backend_from_config(app_dir: Path | None) -> str:
    try:
        configured = str(OpenCompanyConfig.load((app_dir or default_app_dir()).resolve()).sandbox.backend).strip()
    except Exception:
        configured = ""
    return _normalize_sandbox_backend(configured, fallback="anthropic")


def _apply_sandbox_backend(
    orchestrator: Orchestrator,
    backend: str | None,
) -> str:
    default_backend = _default_sandbox_backend_from_config(
        getattr(orchestrator, "app_dir", None)
    )
    backend_name = _normalize_sandbox_backend(backend, fallback=default_backend)
    config = getattr(orchestrator, "config", None)
    tool_executor = getattr(orchestrator, "tool_executor", None)
    if config is not None and hasattr(config, "sandbox"):
        config.sandbox.backend = backend_name
        backend_cls = resolve_sandbox_backend_cls(config.sandbox)
        if tool_executor is not None and hasattr(tool_executor, "sandbox_backend_cls"):
            tool_executor.sandbox_backend_cls = backend_cls
    if tool_executor is not None and hasattr(tool_executor, "_shell_backend_instance"):
        tool_executor._shell_backend_instance = None
    return backend_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencompany")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sandbox_backend_choices = _available_sandbox_backend_names()

    def add_remote_options(target_parser: argparse.ArgumentParser) -> None:
        target_parser.add_argument(
            "--remote-target",
            default=None,
            help="Remote SSH target in user@host[:port] format (direct mode only).",
        )
        target_parser.add_argument(
            "--remote-dir",
            default=None,
            help="Remote Linux absolute directory used as workspace root (direct mode only).",
        )
        target_parser.add_argument(
            "--remote-auth",
            choices=["key", "password"],
            default=None,
            help="Remote auth mode (defaults to key when --remote-target is set).",
        )
        target_parser.add_argument(
            "--remote-key-path",
            default=None,
            help="SSH identity file path when --remote-auth=key.",
        )
        target_parser.add_argument(
            "--remote-known-hosts",
            choices=["accept_new", "strict"],
            default="accept_new",
            help="Host key verification policy for remote SSH.",
        )

    run_parser = subparsers.add_parser(
        "run",
        help="Run a task and persist runtime events/messages for the session",
    )
    run_parser.add_argument("task", help="Task for the root coordinator")
    run_parser.add_argument("--project-dir", default=".", help="Target project directory")
    run_parser.add_argument(
        "--workspace-mode",
        choices=["direct", "staged"],
        default=None,
        help="Workspace mode for new sessions (defaults to direct).",
    )
    run_parser.add_argument(
        "--sandbox-backend",
        choices=sandbox_backend_choices,
        default=None,
        help="Override sandbox backend for this run only (defaults to [sandbox].backend).",
    )
    run_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    run_parser.add_argument("--locale", default=None, help="Locale: en or zh")
    run_parser.add_argument("--model", default=None, help="Override model for this run")
    run_parser.add_argument(
        "--root-agent-name",
        default=None,
        help="Optional custom display name for the root coordinator in this run.",
    )
    run_parser.add_argument(
        "--preview-chars",
        type=int,
        default=_RUN_PREVIEW_CHARS_DEFAULT,
        help=(
            "Max preview chars per content block for live run panel in TTY mode "
            f"(default: {_RUN_PREVIEW_CHARS_DEFAULT})."
        ),
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help="Write API request/response debug logs to .opencompany/sessions/<session_id>/debug/ (split by agent+module).",
    )
    add_remote_options(run_parser)

    resume_parser = subparsers.add_parser(
        "resume",
        help="Continue an existing session by appending a new instruction",
    )
    resume_parser.add_argument("session_id", help="Session ID")
    resume_parser.add_argument("instruction", help="New instruction appended as a root user message")
    resume_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    resume_parser.add_argument("--locale", default=None, help="Locale: en or zh")
    resume_parser.add_argument(
        "--sandbox-backend",
        choices=sandbox_backend_choices,
        default=None,
        help="Override sandbox backend for this resume only (defaults to [sandbox].backend).",
    )
    resume_parser.add_argument("--model", default=None, help="Override model for this resume")
    resume_parser.add_argument(
        "--preview-chars",
        type=int,
        default=_RUN_PREVIEW_CHARS_DEFAULT,
        help=(
            "Max preview chars per content block for live run panel in TTY mode "
            f"(default: {_RUN_PREVIEW_CHARS_DEFAULT})."
        ),
    )
    resume_parser.add_argument(
        "--debug",
        action="store_true",
        help="Write API request/response debug logs to .opencompany/sessions/<session_id>/debug/ (split by agent+module).",
    )

    clone_parser = subparsers.add_parser(
        "clone",
        help="Clone an existing non-running session into a new branch session",
    )
    clone_parser.add_argument("session_id", help="Source session ID")
    clone_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    clone_parser.add_argument("--locale", default=None, help="Locale: en or zh")
    clone_parser.add_argument(
        "--debug",
        action="store_true",
        help="Write API request/response debug logs to .opencompany/sessions/<session_id>/debug/ (split by agent+module).",
    )

    export_parser = subparsers.add_parser(
        "export-logs",
        help="Export session bundle (events + messages + diagnostics + tool-run metrics)",
    )
    export_parser.add_argument("session_id", help="Session ID")
    export_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    export_parser.add_argument(
        "--export-path",
        default=None,
        help="Optional output file path (defaults to .opencompany/sessions/<session_id>/export.json)",
    )

    messages_parser = subparsers.add_parser(
        "messages",
        help="List persisted session messages (message-first live stream source)",
    )
    messages_parser.add_argument("session_id", help="Session ID")
    messages_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    messages_parser.add_argument(
        "--agent-id",
        default=None,
        help="Optional agent ID filter",
    )
    messages_parser.add_argument(
        "--tail",
        type=int,
        default=200,
        help="Return the most recent N messages when cursor is not provided",
    )
    messages_parser.add_argument(
        "--cursor",
        default=None,
        help="Opaque cursor from previous page for incremental reads",
    )
    messages_parser.add_argument(
        "--include-extra",
        action="store_true",
        help="Include non-message runtime events (shell/tool-run/control/protocol)",
    )
    messages_parser.add_argument(
        "--format",
        dest="format",
        choices=["json", "text"],
        default="json",
        help="Output format",
    )

    tool_runs_parser = subparsers.add_parser(
        "tool-runs",
        help="List persisted tool runs for a session (cursor-paged JSON)",
    )
    tool_runs_parser.add_argument("session_id", help="Session ID")
    tool_runs_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    tool_runs_parser.add_argument(
        "--status",
        default=None,
        help="Optional status filter: queued/running/completed/failed/cancelled",
    )
    tool_runs_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum rows to print (defaults to runtime.tools.list_default_limit).",
    )
    tool_runs_parser.add_argument(
        "--cursor",
        default=None,
        help="Opaque cursor from previous page.",
    )

    tool_run_metrics_parser = subparsers.add_parser(
        "tool-run-metrics",
        help="Export tool-run metrics (durations, failure rates) for a session",
    )
    tool_run_metrics_parser.add_argument("session_id", help="Session ID")
    tool_run_metrics_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    tool_run_metrics_parser.add_argument(
        "--export",
        action="store_true",
        help="Write metrics JSON to the session directory instead of printing JSON.",
    )
    tool_run_metrics_parser.add_argument(
        "--export-path",
        default=None,
        help="Optional output file path when used with --export (defaults to .opencompany/sessions/<session_id>/tool_run_metrics.json).",
    )

    apply_parser = subparsers.add_parser(
        "apply", help="Apply staged workspace changes to the target project directory"
    )
    apply_parser.add_argument("session_id", help="Session ID")
    apply_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    apply_parser.add_argument("--yes", action="store_true", help="Apply without interactive confirmation")

    undo_parser = subparsers.add_parser(
        "undo", help="Undo the last applied project sync for a session"
    )
    undo_parser.add_argument("session_id", help="Session ID")
    undo_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    undo_parser.add_argument("--yes", action="store_true", help="Undo without interactive confirmation")

    terminal_parser = subparsers.add_parser(
        "terminal",
        help="Open a persistent system terminal for a session workspace",
    )
    terminal_parser.add_argument("session_id", help="Session ID")
    terminal_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    terminal_parser.add_argument(
        "--self-check",
        action="store_true",
        help="Verify terminal sandbox policy parity with agent shell and enforceability.",
    )

    tui_parser = subparsers.add_parser("tui", help="Launch the terminal UI")
    tui_parser.add_argument("--project-dir", default=None, help="Target project directory")
    tui_parser.add_argument("--session-id", default=None, help="Session ID to resume inside the TUI")
    tui_parser.add_argument(
        "--workspace-mode",
        choices=["direct", "staged"],
        default=None,
        help="Workspace mode for new sessions created from the TUI (defaults to direct).",
    )
    tui_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    tui_parser.add_argument("--locale", default=None, help="Locale: en or zh")
    tui_parser.add_argument(
        "--debug",
        action="store_true",
        help="Write API request/response debug logs to .opencompany/sessions/<session_id>/debug/ (split by agent+module).",
    )
    add_remote_options(tui_parser)

    ui_parser = subparsers.add_parser("ui", help="Launch the local Web UI")
    ui_parser.add_argument("--project-dir", default=None, help="Target project directory")
    ui_parser.add_argument("--session-id", default=None, help="Session ID to resume inside the UI")
    ui_parser.add_argument(
        "--workspace-mode",
        choices=["direct", "staged"],
        default=None,
        help="Workspace mode for new sessions created from the Web UI (defaults to direct).",
    )
    ui_parser.add_argument("--app-dir", default=None, help="OpenCompany app directory")
    ui_parser.add_argument("--locale", default=None, help="Locale: en or zh")
    ui_parser.add_argument(
        "--debug",
        action="store_true",
        help="Write API request/response debug logs to .opencompany/sessions/<session_id>/debug/ (split by agent+module).",
    )
    add_remote_options(ui_parser)
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host for the UI HTTP server")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port for the UI HTTP server")
    return parser


@dataclass(slots=True)
class _AgentView:
    id: str
    name: str
    role: str
    status: str
    step_count: int
    last_activity_at: str | None
    latest_message: str
    running_tool_runs: int
    queued_tool_runs: int
    failed_tool_runs: int
    output_tokens: int
    parent_agent_id: str | None
    children: list[str]
    goal: str
    summary: str
    model: str = "-"


_RUN_STATUS_REFRESH_SECONDS = 0.25
_RUN_PAGE_SWITCH_SECONDS = 5.0
_RUN_SPINNER_FRAMES = ("⌛️", "⏳")
_RUN_PREVIEW_CHARS_DEFAULT = 256
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ANSI_RESET = "\x1b[0m"
_ANSI_COLOR_BY_STATUS = {
    "running": "\x1b[36m",
    "queued": "\x1b[33m",
    "pending": "\x1b[33m",
    "paused": "\x1b[94m",
    "completed": "\x1b[32m",
    "failed": "\x1b[31m",
    "cancelled": "\x1b[35m",
    "terminated": "\x1b[35m",
    "interrupted": "\x1b[33m",
    "starting": "\x1b[34m",
    "resuming": "\x1b[34m",
}
_ANSI_COLOR_BY_TYPE = {
    "meta": "\x1b[90m",
    "session": "\x1b[36m",
    "name": "\x1b[37m",
    "role": "\x1b[35m",
    "stats": "\x1b[94m",
    "step": "\x1b[95m",
    "activity": "\x1b[94m",
    "latest": "\x1b[35m",
    "tools": "\x1b[96m",
    "tokens": "\x1b[92m",
    "model": "\x1b[96m",
    "parent": "\x1b[34m",
    "children": "\x1b[36m",
    "goal": "\x1b[33m",
    "summary": "\x1b[32m",
}
_RUN_PANEL_LABELS = {
    "en": {
        "elapsed": "elapsed",
        "mode": "mode",
        "mode_run": "run",
        "mode_resume": "resume",
        "page": "page",
        "session": "session",
        "status": "status",
        "agents": "agents",
        "stats": "stats",
        "lineage": "lineage",
        "step": "step",
        "activity": "active",
        "latest": "latest",
        "tools": "tools",
        "tools_running": "running",
        "tools_queued": "queued",
        "tools_failed": "failed",
        "output_tokens": "out_tok",
        "model": "model",
        "parent": "parent",
        "children": "children",
        "goal": "goal",
        "summary": "summary",
        "none": "-",
    },
    "zh": {
        "elapsed": "耗时",
        "mode": "模式",
        "mode_run": "运行",
        "mode_resume": "恢复",
        "page": "页",
        "session": "会话",
        "status": "状态",
        "agents": "Agent数",
        "stats": "概览",
        "lineage": "关系",
        "step": "步骤",
        "activity": "活动",
        "latest": "最新活动",
        "tools": "工具",
        "tools_running": "运行中",
        "tools_queued": "排队",
        "tools_failed": "失败",
        "output_tokens": "输出Token",
        "model": "模型",
        "parent": "父",
        "children": "子",
        "goal": "目标",
        "summary": "总结",
        "none": "-",
    },
}
_RUN_STATUS_LABELS = {
    "en": {
        "starting": "starting",
        "resuming": "resuming",
        "queued": "queued",
        "pending": "pending",
        "running": "running",
        "paused": "paused",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "terminated": "terminated",
        "interrupted": "interrupted",
    },
    "zh": {
        "starting": "启动中",
        "resuming": "恢复中",
        "queued": "排队",
        "pending": "待处理",
        "running": "运行中",
        "paused": "已暂停",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "terminated": "已终止",
        "interrupted": "已中断",
    },
}
_RUN_ROLE_LABELS = {
    "en": {
        "root": "root",
        "worker": "worker",
    },
    "zh": {
        "root": "根Agent",
        "worker": "工作Agent",
    },
}


def _run_status_panel_enabled() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _no_color_requested() -> bool:
    return "NO_COLOR" in os.environ


def _truncate_inline(text: str, max_chars: int) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return "-"
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3] + "..."


def _with_ellipsis(text: str, width: int) -> str:
    clean = str(text or "").rstrip()
    if width <= 3:
        return "." * max(0, width)
    if _display_width(clean) <= width:
        return clean
    available = max(1, width - 3)
    head = _truncate_display(clean, available)
    return f"{head.rstrip()}..."


def _fit_plain(text: str, width: int) -> str:
    normalized = str(text or "").replace("\n", " ").replace("\r", " ")
    width = max(1, int(width))
    if _display_width(normalized) <= width:
        return normalized
    return _with_ellipsis(normalized, width)


def _char_display_width(char: str) -> int:
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def _display_width(text: str) -> int:
    plain = _ANSI_ESCAPE_RE.sub("", str(text or ""))
    return sum(_char_display_width(char) for char in plain)


def _truncate_display(text: str, width: int) -> str:
    width = max(1, int(width))
    current_width = 0
    result: list[str] = []
    for char in str(text or ""):
        char_width = _char_display_width(char)
        if current_width + char_width > width:
            break
        result.append(char)
        current_width += char_width
    return "".join(result)


def _wrap_display(text: str, width: int) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return [""]
    width = max(1, int(width))
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    for char in normalized:
        char_width = _char_display_width(char)
        if current and current_width + char_width > width:
            lines.append("".join(current).rstrip())
            current = [char]
            current_width = char_width
            continue
        current.append(char)
        current_width += char_width
    if current:
        lines.append("".join(current).rstrip())
    return lines or [""]


class _RunStatusPanel:
    def __init__(
        self,
        *,
        mode: str,
        locale: str,
        initial_session_id: str | None = None,
        initial_status: str | None = None,
        stream: TextIO | None = None,
        refresh_seconds: float | None = None,
        preview_chars: int = _RUN_PREVIEW_CHARS_DEFAULT,
    ) -> None:
        self.mode = "resume" if mode == "resume" else "run"
        self.locale = locale if locale in {"en", "zh"} else "en"
        self.labels = _RUN_PANEL_LABELS.get(self.locale, _RUN_PANEL_LABELS["en"])
        self.status_labels = _RUN_STATUS_LABELS.get(self.locale, _RUN_STATUS_LABELS["en"])
        self.role_labels = _RUN_ROLE_LABELS.get(self.locale, _RUN_ROLE_LABELS["en"])
        self.stream = stream or sys.stdout
        refresh_value = _RUN_STATUS_REFRESH_SECONDS if refresh_seconds is None else refresh_seconds
        self.refresh_seconds = max(0.05, float(refresh_value))
        try:
            normalized_preview = int(preview_chars)
        except (TypeError, ValueError):
            normalized_preview = _RUN_PREVIEW_CHARS_DEFAULT
        self.preview_chars = max(16, normalized_preview)
        self.supports_ansi = bool(getattr(self.stream, "isatty", lambda: False)())
        self.use_color = self.supports_ansi and not _no_color_requested()
        self._orchestrator: Orchestrator | None = None
        self._session_id = (initial_session_id or "").strip() or None
        self._session_status = (
            (initial_status or "").strip().lower()
            or ("resuming" if self.mode == "resume" else "starting")
        )
        self._started_at = time.perf_counter()
        self._render_task: asyncio.Task[None] | None = None
        self._running = False
        self._printed_rows = 0
        self._frame = 0
        self._message_metrics_cache: dict[str, tuple[int, int, int, str | None, str | None]] = {}
        self._manual_page_index: int | None = None
        self._page_shift_request = 0
        self._keyboard_enabled = False
        self._stdin_fd: int | None = None
        self._stdin_termios_attrs: Any = None
        self._panel_header_line_count = 2

    def attach(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        orchestrator.subscribe(self._on_event)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self.supports_ansi:
            self._enable_keyboard_controls()
            self.stream.write("\x1b[?25l")
            self.stream.flush()
        self._render_task = asyncio.create_task(self._render_loop())

    async def stop(
        self,
        *,
        final_session_id: str | None = None,
        final_status: str | None = None,
    ) -> None:
        if final_session_id:
            self._session_id = final_session_id
        if final_status:
            self._session_status = final_status
        self._running = False
        if self._render_task is not None and not self._render_task.done():
            self._render_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._render_task
        self._render_task = None
        self._render_once()
        if self.supports_ansi:
            self._disable_keyboard_controls()
            self.stream.write("\x1b[?25h")
            self.stream.flush()

    async def _render_loop(self) -> None:
        while self._running:
            self._render_once()
            await asyncio.sleep(self.refresh_seconds)

    def _render_once(self) -> None:
        session_id, session_status, agents = self._snapshot()
        self._consume_page_key_inputs()
        terminal_columns = self._terminal_columns()
        terminal_rows = self._terminal_rows()
        all_lines = self._build_lines(
            session_id=session_id,
            session_status=session_status,
            agents=agents,
            max_total_width=max(1, terminal_columns - 1),
        )
        lines, page_index, page_count = self._paginate_for_terminal(
            all_lines,
            terminal_columns=terminal_columns,
            terminal_rows=terminal_rows,
        )
        if page_count > 1:
            page_hint = f"{page_index + 1}/{page_count}(-/=)"
            lines, _, _ = self._paginate_for_terminal(
                self._build_lines(
                    session_id=session_id,
                    session_status=session_status,
                    agents=agents,
                    max_total_width=max(1, terminal_columns - 1),
                    page_hint=page_hint,
                ),
                terminal_columns=terminal_columns,
                terminal_rows=terminal_rows,
                force_page_index=page_index,
            )
        if self.supports_ansi and self._printed_rows > 0:
            self.stream.write(f"\x1b[{self._printed_rows}F")
            self.stream.write("\x1b[J")
        self.stream.write("\n".join(lines))
        self.stream.write("\n")
        self.stream.flush()
        self._printed_rows = self._count_visual_rows(lines=lines, terminal_columns=terminal_columns)
        self._frame += 1

    def _enable_keyboard_controls(self) -> None:
        if termios is None or tty is None:
            return
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not bool(getattr(stdin, "isatty", lambda: False)()):
            return
        try:
            fd = stdin.fileno()
        except Exception:
            return
        try:
            attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            return
        self._keyboard_enabled = True
        self._stdin_fd = fd
        self._stdin_termios_attrs = attrs

    def _disable_keyboard_controls(self) -> None:
        if not self._keyboard_enabled:
            return
        fd = self._stdin_fd
        attrs = self._stdin_termios_attrs
        self._keyboard_enabled = False
        self._stdin_fd = None
        self._stdin_termios_attrs = None
        if termios is None or fd is None or attrs is None:
            return
        with suppress(Exception):
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

    def _consume_page_key_inputs(self) -> None:
        if not self._keyboard_enabled:
            return
        fd = self._stdin_fd
        if fd is None:
            return
        while True:
            try:
                readable, _, _ = select.select([fd], [], [], 0.0)
            except Exception:
                return
            if not readable:
                return
            try:
                chunk = os.read(fd, 64)
            except BlockingIOError:
                return
            except OSError:
                return
            if not chunk:
                return
            self._apply_page_key_chunk(chunk)

    def _apply_page_key_chunk(self, chunk: bytes) -> None:
        for char in chunk.decode("utf-8", errors="ignore"):
            if char in {"=", "+"}:
                self._page_shift_request += 1
            elif char == "-":
                self._page_shift_request -= 1

    @staticmethod
    def _terminal_columns() -> int:
        try:
            return max(6, int(shutil.get_terminal_size((160, 40)).columns))
        except Exception:
            return 160

    @staticmethod
    def _terminal_rows() -> int:
        try:
            return max(6, int(shutil.get_terminal_size((160, 40)).lines))
        except Exception:
            return 40

    @staticmethod
    def _count_visual_rows(*, lines: list[str], terminal_columns: int) -> int:
        columns = max(1, int(terminal_columns))
        total = 0
        for line in lines:
            width = _display_width(line)
            total += max(1, (max(1, width) + columns - 1) // columns)
        return total

    def _paginate_for_terminal(
        self,
        lines: list[str],
        *,
        terminal_columns: int,
        terminal_rows: int,
        force_page_index: int | None = None,
    ) -> tuple[list[str], int, int]:
        if not lines:
            return [], 0, 1
        budget_rows = max(1, int(terminal_rows) - 1)
        total_rows = self._count_visual_rows(lines=lines, terminal_columns=terminal_columns)
        if total_rows <= budget_rows:
            return lines, 0, 1
        header_count = min(max(1, int(self._panel_header_line_count)), len(lines))
        header = lines[:header_count]
        body = lines[header_count:]
        header_rows = self._count_visual_rows(lines=header, terminal_columns=terminal_columns)
        if header_rows >= budget_rows:
            clipped = self._clip_lines_to_rows(
                header,
                terminal_columns=terminal_columns,
                max_rows=budget_rows,
            )
            return clipped, 0, 1
        body_budget_rows = max(1, budget_rows - header_rows)
        pages = self._split_pages(
            body,
            terminal_columns=terminal_columns,
            page_body_rows=body_budget_rows,
        )
        if not pages:
            pages = [[]]
        total_pages = len(pages)
        if force_page_index is not None:
            page_index = int(force_page_index) % total_pages
        else:
            if self._page_shift_request != 0:
                base = (
                    self._current_page_index(total_pages)
                    if self._manual_page_index is None
                    else int(self._manual_page_index)
                )
                self._manual_page_index = (base + int(self._page_shift_request)) % total_pages
                self._page_shift_request = 0
            if self._manual_page_index is not None:
                page_index = int(self._manual_page_index) % total_pages
            else:
                page_index = self._current_page_index(total_pages)
        if total_pages <= 1:
            self._manual_page_index = None
        return header + pages[page_index], page_index, total_pages

    def _current_page_index(self, page_count: int) -> int:
        if page_count <= 1:
            return 0
        elapsed = max(0.0, time.perf_counter() - self._started_at)
        return int(elapsed / _RUN_PAGE_SWITCH_SECONDS) % page_count

    @classmethod
    def _split_pages(
        cls,
        lines: list[str],
        *,
        terminal_columns: int,
        page_body_rows: int,
    ) -> list[list[str]]:
        if not lines:
            return [[]]
        pages: list[list[str]] = []
        current: list[str] = []
        current_rows = 0
        for line in lines:
            line_rows = cls._count_visual_rows(lines=[line], terminal_columns=terminal_columns)
            if current and current_rows + line_rows > page_body_rows:
                pages.append(current)
                current = [line]
                current_rows = line_rows
                continue
            if not current and line_rows > page_body_rows:
                clipped = cls._clip_lines_to_rows(
                    [line],
                    terminal_columns=terminal_columns,
                    max_rows=page_body_rows,
                )
                pages.append(clipped)
                current = []
                current_rows = 0
                continue
            current.append(line)
            current_rows += line_rows
        if current:
            pages.append(current)
        return pages

    @classmethod
    def _clip_lines_to_rows(
        cls,
        lines: list[str],
        *,
        terminal_columns: int,
        max_rows: int,
    ) -> list[str]:
        clipped: list[str] = []
        rows = 0
        for line in lines:
            line_rows = cls._count_visual_rows(lines=[line], terminal_columns=terminal_columns)
            if rows + line_rows > max_rows:
                break
            clipped.append(line)
            rows += line_rows
        return clipped

    def _snapshot(self) -> tuple[str | None, str, list[_AgentView]]:
        orchestrator = self._orchestrator
        session_id = self._session_id
        if session_id is None and orchestrator is not None:
            latest = str(orchestrator.latest_session_id or "").strip()
            if latest:
                session_id = latest
                self._session_id = latest
        session_status = self._session_status
        agents: list[_AgentView] = []
        if orchestrator is None or session_id is None:
            return session_id, session_status, agents
        try:
            session_row = orchestrator.storage.load_session(session_id)
            if isinstance(session_row, dict):
                session_status_raw = str(session_row.get("status", "")).strip().lower()
                if session_status_raw:
                    session_status = session_status_raw
                    self._session_status = session_status_raw
            load_agents = getattr(orchestrator, "load_session_agents", None)
            if callable(load_agents):
                raw_agent_rows = load_agents(session_id)
            else:
                raw_agent_rows = orchestrator.storage.load_agents(session_id)
            agent_rows = [row for row in raw_agent_rows if isinstance(row, dict)]
            agent_ids = [
                str(row.get("id", "")).strip()
                for row in agent_rows
                if str(row.get("id", "")).strip()
            ]
            last_event_at_by_agent = self._last_event_at_by_agent(orchestrator, session_id)
            tool_stats_by_agent = self._tool_stats_by_agent(orchestrator, session_id)
            message_stats_by_agent = self._message_stats_by_agent(
                orchestrator,
                session_id,
                agent_ids,
            )
            for row in agent_rows:
                if not isinstance(row, dict):
                    continue
                agent = self._agent_view_from_row(
                    row,
                    last_event_at_by_agent=last_event_at_by_agent,
                    tool_stats_by_agent=tool_stats_by_agent,
                    message_stats_by_agent=message_stats_by_agent,
                )
                if agent is not None:
                    agents.append(agent)
        except Exception:
            pass
        agents.sort(key=lambda item: (0 if item.role == "root" else 1, item.name.lower(), item.id))
        return session_id, session_status, agents

    def _agent_view_from_row(
        self,
        row: dict[str, Any],
        *,
        last_event_at_by_agent: dict[str, str],
        tool_stats_by_agent: dict[str, dict[str, Any]],
        message_stats_by_agent: dict[str, dict[str, Any]],
    ) -> _AgentView | None:
        agent_id = str(row.get("id", "")).strip()
        if not agent_id:
            return None
        role = str(row.get("role", "")).strip().lower()
        status_raw = str(row.get("status", "")).strip().lower()
        status = status_raw
        children = self._decode_children(
            row.get("children_json")
            if "children_json" in row
            else row.get("children")
        )
        metadata = self._decode_metadata(row.get("metadata_json"))
        model = (
            str(row.get("model", "")).strip()
            or str(metadata.get("model", "")).strip()
            or self.labels["none"]
        )
        summary = str(row.get("summary") or "").strip() or self.labels["none"]
        try:
            step_count = max(0, int(row.get("step_count") or 0))
        except (TypeError, ValueError):
            step_count = 0
        message_stats = message_stats_by_agent.get(agent_id, {})
        tool_stats = tool_stats_by_agent.get(agent_id, {})
        last_activity_at = self._latest_timestamp(
            last_event_at_by_agent.get(agent_id),
            str(tool_stats.get("last_activity_at") or "").strip() or None,
            str(message_stats.get("last_activity_at") or "").strip() or None,
        )
        try:
            running_tool_runs = max(0, int(tool_stats.get("running", 0) or 0))
        except (TypeError, ValueError):
            running_tool_runs = 0
        try:
            queued_tool_runs = max(0, int(tool_stats.get("queued", 0) or 0))
        except (TypeError, ValueError):
            queued_tool_runs = 0
        try:
            failed_tool_runs = max(0, int(tool_stats.get("failed", 0) or 0))
        except (TypeError, ValueError):
            failed_tool_runs = 0
        try:
            output_tokens = max(0, int(message_stats.get("output_tokens", 0) or 0))
        except (TypeError, ValueError):
            output_tokens = 0
        latest_message = str(message_stats.get("latest_message") or "").strip() or self.labels["none"]
        return _AgentView(
            id=agent_id,
            name=str(row.get("name", "")).strip() or agent_id,
            role=role or "worker",
            status=status or "pending",
            step_count=step_count,
            last_activity_at=last_activity_at,
            latest_message=latest_message,
            running_tool_runs=running_tool_runs,
            queued_tool_runs=queued_tool_runs,
            failed_tool_runs=failed_tool_runs,
            output_tokens=output_tokens,
            parent_agent_id=str(row.get("parent_agent_id") or "").strip() or None,
            children=children,
            model=model,
            goal=str(row.get("instruction", "")).strip() or self.labels["none"],
            summary=summary,
        )

    @staticmethod
    def _decode_children(raw_children: Any) -> list[str]:
        if isinstance(raw_children, list):
            return [str(item).strip() for item in raw_children if str(item).strip()]
        if not isinstance(raw_children, str):
            return []
        try:
            parsed = json.loads(raw_children)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    @staticmethod
    def _decode_metadata(raw_metadata: Any) -> dict[str, Any]:
        if isinstance(raw_metadata, dict):
            return raw_metadata
        if not isinstance(raw_metadata, str):
            return {}
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _last_event_at_by_agent(self, orchestrator: Orchestrator, session_id: str) -> dict[str, str]:
        storage = getattr(orchestrator, "storage", None)
        connection = getattr(storage, "connection", None)
        if connection is None:
            return {}
        try:
            rows = connection.execute(
                """
                SELECT agent_id, MAX(timestamp) AS last_event_at
                FROM events
                WHERE session_id = ?
                  AND agent_id IS NOT NULL
                  AND agent_id != ''
                GROUP BY agent_id
                """,
                (session_id,),
            ).fetchall()
        except Exception:
            return {}
        result: dict[str, str] = {}
        for row in rows:
            agent_id = str(row["agent_id"] if "agent_id" in row.keys() else "").strip()
            if not agent_id:
                continue
            last_event_at = str(row["last_event_at"] if "last_event_at" in row.keys() else "").strip()
            if last_event_at:
                result[agent_id] = last_event_at
        return result

    def _tool_stats_by_agent(self, orchestrator: Orchestrator, session_id: str) -> dict[str, dict[str, Any]]:
        storage = getattr(orchestrator, "storage", None)
        connection = getattr(storage, "connection", None)
        if connection is None:
            return {}
        try:
            rows = connection.execute(
                """
                SELECT
                    agent_id,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    MAX(COALESCE(completed_at, started_at, created_at)) AS last_tool_at
                FROM tool_runs
                WHERE session_id = ?
                GROUP BY agent_id
                """,
                (session_id,),
            ).fetchall()
        except Exception:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            agent_id = str(row["agent_id"] if "agent_id" in row.keys() else "").strip()
            if not agent_id:
                continue
            result[agent_id] = {
                "running": int(row["running_count"] or 0),
                "queued": int(row["queued_count"] or 0),
                "failed": int(row["failed_count"] or 0),
                "last_activity_at": str(row["last_tool_at"] or "").strip() or None,
            }
        return result

    def _message_stats_by_agent(
        self,
        orchestrator: Orchestrator,
        session_id: str,
        agent_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not agent_ids:
            return {}
        session_path = None
        try:
            session_path = orchestrator.paths.existing_session_dir(session_id)
        except Exception:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for agent_id in agent_ids:
            if not agent_id:
                continue
            path = session_path / f"{agent_id}_messages.jsonl"
            metrics = self._message_stats_for_path(path)
            result[agent_id] = {
                "output_tokens": int(metrics.get("output_tokens", 0) or 0),
                "last_activity_at": str(metrics.get("last_activity_at") or "").strip() or None,
                "latest_message": str(metrics.get("latest_message") or "").strip() or self.labels["none"],
            }
        return result

    def _message_stats_for_path(self, path: Path) -> dict[str, Any]:
        cache_key = str(path.resolve())
        try:
            stat = path.stat()
        except OSError:
            self._message_metrics_cache.pop(cache_key, None)
            return {"output_tokens": 0, "last_activity_at": None, "latest_message": self.labels["none"]}
        size = int(stat.st_size)
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
        cached = self._message_metrics_cache.get(cache_key)
        if cached is not None:
            cached_size, cached_mtime_ns, cached_tokens, cached_last_activity, cached_latest_message = cached
            if cached_size == size and cached_mtime_ns == mtime_ns:
                return {
                    "output_tokens": cached_tokens,
                    "last_activity_at": cached_last_activity,
                    "latest_message": cached_latest_message,
                }
        output_tokens = 0
        last_activity_at: str | None = None
        last_activity_dt: datetime | None = None
        latest_message = self.labels["none"]
        latest_record: dict[str, Any] | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    latest_record = record
                    output_tokens += self._record_output_tokens(record)
                    timestamp = str(record.get("timestamp", "")).strip()
                    timestamp_dt = self._parse_timestamp(timestamp)
                    if timestamp_dt is None:
                        continue
                    if last_activity_dt is None or timestamp_dt > last_activity_dt:
                        last_activity_dt = timestamp_dt
                        last_activity_at = timestamp
        except OSError:
            return {"output_tokens": 0, "last_activity_at": None, "latest_message": self.labels["none"]}
        if latest_record is not None:
            latest_message = self._message_preview_from_record(latest_record)
        self._message_metrics_cache[cache_key] = (
            size,
            mtime_ns,
            output_tokens,
            last_activity_at,
            latest_message,
        )
        return {
            "output_tokens": output_tokens,
            "last_activity_at": last_activity_at,
            "latest_message": latest_message,
        }

    @classmethod
    def _record_output_tokens(cls, record: dict[str, Any]) -> int:
        response = record.get("response")
        usage_sources: list[Any] = []
        if isinstance(response, dict):
            usage_sources.append(response.get("usage"))
        usage_sources.append(record.get("usage"))
        usage_sources.append(record.get("token_usage"))
        for usage in usage_sources:
            value = cls._usage_output_tokens(usage)
            if value > 0:
                return value
        return 0

    @classmethod
    def _message_preview_from_record(cls, record: dict[str, Any]) -> str:
        message = record.get("message")
        role = str(record.get("role", "")).strip() or "message"
        preview = ""
        if isinstance(message, dict):
            role = str(message.get("role", "")).strip() or role
            preview = cls._message_content_preview(message.get("content"))
            if not preview:
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    first_name = ""
                    first = tool_calls[0]
                    if isinstance(first, dict):
                        function = first.get("function")
                        if isinstance(function, dict):
                            first_name = str(function.get("name", "")).strip()
                    preview = f"tool_calls={len(tool_calls)}"
                    if first_name:
                        preview += f" ({first_name})"
                tool_call_id = str(message.get("tool_call_id", "")).strip()
                if not preview and tool_call_id:
                    preview = f"tool_call_id={tool_call_id}"
        if not preview:
            preview = cls._message_content_preview(record.get("content"))
        normalized_preview = " ".join(preview.split()) or "-"
        return f"{role}: {normalized_preview}"

    @staticmethod
    def _message_content_preview(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments: list[str] = []
            for item in content:
                if isinstance(item, str):
                    fragments.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                for key in ("text", "content"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        fragments.append(value)
                        break
            return " ".join(fragments)
        if isinstance(content, dict):
            for key in ("text", "content", "summary", "reasoning"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            try:
                return json.dumps(content, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(content)
        if content is None:
            return ""
        return str(content)

    @classmethod
    def _usage_output_tokens(cls, usage: Any) -> int:
        if not isinstance(usage, dict):
            return 0
        for key in ("output_tokens", "completion_tokens", "generated_tokens"):
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

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        return parsed

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @classmethod
    def _latest_timestamp(cls, *values: str | None) -> str | None:
        latest_raw: str | None = None
        latest_dt: datetime | None = None
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            parsed = cls._parse_timestamp(text)
            if parsed is None:
                continue
            if latest_dt is None or parsed > latest_dt:
                latest_dt = parsed
                latest_raw = text
        return latest_raw

    def _build_lines(
        self,
        *,
        session_id: str | None,
        session_status: str,
        agents: list[_AgentView],
        max_total_width: int | None = None,
        page_hint: str | None = None,
    ) -> list[str]:
        if max_total_width is None:
            columns = max(1, self._terminal_columns() - 1)
        else:
            columns = max(1, int(max_total_width))
        elapsed = self._format_elapsed()
        spinner = _RUN_SPINNER_FRAMES[self._frame % len(_RUN_SPINNER_FRAMES)]
        mode_text = self.labels["mode_resume"] if self.mode == "resume" else self.labels["mode_run"]
        status_text = self._status_text(session_status)
        header_line_1 = _fit_plain(
            (
                f"{spinner} "
                f"{self.labels['elapsed']}={elapsed} "
                f"{self.labels['mode']}={mode_text}"
                + (
                    f" {self.labels['page']}={page_hint}"
                    if str(page_hint or "").strip()
                    else ""
                )
            ),
            columns,
        )
        header_line_2_plain = _fit_plain(
            (
                f"{self.labels['session']}={session_id or self.labels['none']} "
                f"{self.labels['status']}={status_text} "
                f"{self.labels['agents']}={len(agents)}"
            ),
            columns,
        )
        status_token = f"{self.labels['status']}={status_text}"
        if status_token in header_line_2_plain:
            prefix, suffix = header_line_2_plain.split(status_token, 1)
            header_line_2 = (
                self._colorize_type(prefix, "session")
                + self._colorize_type(f"{self.labels['status']}=", "meta")
                + self._colorize_status(status_text, session_status)
                + self._colorize_type(suffix, "session")
            )
        else:
            header_line_2 = self._colorize_type(header_line_2_plain, "session")
        lines = [
            self._colorize_type(header_line_1, "meta"),
            header_line_2,
        ]
        catalog_text = self._agent_catalog_text(agents)
        catalog_lines = _wrap_display(catalog_text, columns)
        lines.extend(self._colorize_type(line, "session") for line in catalog_lines)
        # Keep only the first two lines sticky across paginated pages.
        self._panel_header_line_count = 2
        for index, agent in enumerate(agents):
            parent = (agent.parent_agent_id or self.labels["none"])[:8]
            children = ",".join(child[:8] for child in agent.children) or self.labels["none"]
            role_text = self.role_labels.get(agent.role, agent.role)
            activity_text = self._activity_text(agent.last_activity_at)
            stats_text = (
                f"{self.labels['step']}={max(0, int(agent.step_count))} "
                f"{self.labels['activity']}={activity_text}"
            )
            tool_text = (
                f"{self.labels['tools_running']}={agent.running_tool_runs} "
                f"{self.labels['tools_queued']}={agent.queued_tool_runs} "
                f"{self.labels['tools_failed']}={agent.failed_tool_runs}"
            )
            lineage_text = (
                f"{self.labels['parent']}={parent} "
                f"{self.labels['children']}={children}"
            )
            agent_name = _fit_plain(
                self._preview_text(f"{agent.name} ({agent.id[:8]})"),
                max(1, columns - 2),
            )
            status_text_colored = self._colorize_status(
                self._status_text(agent.status),
                agent.status,
            )
            title_plain = _fit_plain(
                f"{self._status_text(agent.status)} {agent_name} [{role_text}]",
                columns,
            )
            title_line = title_plain
            status_prefix = f"{self._status_text(agent.status)} "
            if title_plain.startswith(status_prefix):
                title_line = f"{status_text_colored} {self._colorize_type(title_plain[len(status_prefix):], 'name')}"
            role_token = f"[{role_text}]"
            if role_token in title_line:
                title_line = title_line.replace(
                    role_token,
                    self._colorize_type(role_token, "role"),
                    1,
                )
            stats_lines = self._detail_lines(
                label=self.labels["stats"],
                value=stats_text,
                max_total_width=columns,
                max_lines=1,
            )
            latest_lines = self._detail_lines(
                label=self.labels["latest"],
                value=agent.latest_message,
                max_total_width=columns,
                max_lines=3,
            )
            tools_lines = self._detail_lines(
                label=self.labels["tools"],
                value=tool_text,
                max_total_width=columns,
                max_lines=2,
            )
            output_token_lines = self._detail_lines(
                label=self.labels["output_tokens"],
                value=str(max(0, int(agent.output_tokens))),
                max_total_width=columns,
                max_lines=1,
            )
            model_lines = self._detail_lines(
                label=self.labels["model"],
                value=agent.model,
                max_total_width=columns,
                max_lines=2,
            )
            lineage_lines = self._detail_lines(
                label=self.labels["lineage"],
                value=lineage_text,
                max_total_width=columns,
                max_lines=2,
            )
            goal_lines = self._detail_lines(
                label=self.labels["goal"],
                value=agent.goal,
                max_total_width=columns,
                max_lines=3,
            )
            summary_lines = self._detail_lines(
                label=self.labels["summary"],
                value=agent.summary,
                max_total_width=columns,
                max_lines=4,
            )
            lines.extend(
                [
                    title_line,
                    *[self._colorize_type(item, "stats") for item in stats_lines],
                    *[self._colorize_type(item, "latest") for item in latest_lines],
                    *[self._colorize_type(item, "tools") for item in tools_lines],
                    *[self._colorize_type(item, "tokens") for item in output_token_lines],
                    *[self._colorize_type(item, "model") for item in model_lines],
                    *[self._colorize_type(item, "parent") for item in lineage_lines],
                    *[self._colorize_type(item, "goal") for item in goal_lines],
                    *[self._colorize_type(item, "summary") for item in summary_lines],
                ]
            )
            if index != len(agents) - 1:
                lines.append("")
        return lines

    def _agent_catalog_text(self, agents: list[_AgentView]) -> str:
        if not agents:
            return f"[{self.labels['none']}]"
        items: list[str] = []
        for agent in agents:
            name = " ".join(str(agent.name or "").split()) or self.labels["none"]
            agent_id = str(agent.id or "").strip() or self.labels["none"]
            items.append(f"{name}({agent_id})")
        return f"[{', '.join(items)}]"

    def _detail_lines(
        self,
        *,
        label: str,
        value: str,
        max_total_width: int,
        max_lines: int,
    ) -> list[str]:
        width = max(1, int(max_total_width))
        normalized_label = label.strip() or self.labels["none"]
        indent = "  " if width >= 6 else ""
        prefix = f"{indent}{normalized_label}="
        if _display_width(prefix) >= width:
            max_prefix_width = max(0, width - 1)
            prefix = _truncate_display(prefix, max_prefix_width)
        prefix_width = _display_width(prefix)
        continuation_prefix = " " * min(max(0, width - 1), prefix_width)
        available = max(1, width - prefix_width)
        normalized = self._preview_text(value)
        wrapped = _wrap_display(normalized, available)
        if not wrapped:
            wrapped = [self.labels["none"]]
        if len(wrapped) > max(1, max_lines):
            wrapped = wrapped[: max(1, max_lines)]
            wrapped[-1] = _with_ellipsis(wrapped[-1], available)
        lines = [f"{prefix}{wrapped[0]}"]
        for chunk in wrapped[1:]:
            lines.append(f"{continuation_prefix}{chunk}")
        return [_fit_plain(line, width) for line in lines]

    def _preview_text(self, value: str) -> str:
        normalized = " ".join(str(value or "").split()) or self.labels["none"]
        if len(normalized) <= self.preview_chars:
            return normalized
        return _with_ellipsis(normalized, self.preview_chars)

    def _activity_text(self, timestamp: str | None) -> str:
        activity_at = self._parse_timestamp(timestamp)
        if activity_at is None:
            return self.labels["none"]
        elapsed = max(0, int((datetime.now(UTC) - activity_at).total_seconds()))
        if self.locale == "zh":
            if elapsed < 60:
                return f"{elapsed}秒前"
            if elapsed < 3600:
                return f"{elapsed // 60}分钟前"
            if elapsed < 86400:
                return f"{elapsed // 3600}小时前"
            return f"{elapsed // 86400}天前"
        if elapsed < 60:
            return f"{elapsed}s ago"
        if elapsed < 3600:
            return f"{elapsed // 60}m ago"
        if elapsed < 86400:
            return f"{elapsed // 3600}h ago"
        return f"{elapsed // 86400}d ago"

    def _format_elapsed(self) -> str:
        elapsed_seconds = max(0, int(time.perf_counter() - self._started_at))
        hours, remaining = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remaining, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _status_text(self, status: str) -> str:
        normalized = str(status or "").strip().lower() or "pending"
        return self.status_labels.get(normalized, normalized)

    def _colorize_status(self, text: str, status: str) -> str:
        if not self.use_color:
            return text
        color = _ANSI_COLOR_BY_STATUS.get(str(status or "").strip().lower())
        if not color:
            return text
        return f"{color}{text}{_ANSI_RESET}"

    def _colorize_type(self, text: str, content_type: str) -> str:
        if not self.use_color:
            return text
        color = _ANSI_COLOR_BY_TYPE.get(content_type)
        if not color:
            return text
        return f"{color}{text}{_ANSI_RESET}"

    def _on_event(self, record: dict[str, Any]) -> None:
        session_id = str(record.get("session_id", "")).strip()
        if session_id:
            self._session_id = session_id
        payload = record.get("payload")
        if isinstance(payload, dict):
            payload_status = str(payload.get("session_status", "")).strip().lower()
            if payload_status:
                self._session_status = payload_status
        event_type = str(record.get("event_type", "")).strip()
        if event_type in {"session_started", "session_resumed"}:
            self._session_status = "running"
        elif event_type == "session_interrupted":
            self._session_status = "interrupted"
        elif event_type == "session_failed":
            self._session_status = "failed"
        elif event_type == "session_finalized":
            payload_status = (
                str(payload.get("session_status", "")).strip().lower()
                if isinstance(payload, dict)
                else ""
            )
            self._session_status = payload_status or "completed"


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def _resolve_export_file_path(raw_path: str | None, *, flag_name: str) -> Path | None:
    if raw_path is None:
        return None
    normalized = raw_path.strip()
    if not normalized:
        raise SystemExit(f"{flag_name} must be a file path.")
    # Trailing slash/backslash indicates a directory-like path.
    if normalized.endswith(("/", "\\")):
        raise SystemExit(f"{flag_name} must be a file path, not a directory path: {raw_path}")
    candidate = Path(normalized).expanduser()
    if candidate.exists() and candidate.is_dir():
        raise SystemExit(f"{flag_name} must be a file path, not a directory: {raw_path}")
    return candidate


def _normalize_session_id_or_exit(session_id: str) -> str:
    try:
        return RuntimePaths.normalize_session_id(session_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _remote_cli_config_from_args(
    args: argparse.Namespace,
    *,
    command_name: str,
) -> tuple[RemoteSessionConfig | None, str | None]:
    remote_target = str(getattr(args, "remote_target", None) or "").strip()
    remote_dir = str(getattr(args, "remote_dir", None) or "").strip()
    remote_auth = str(getattr(args, "remote_auth", None) or "").strip().lower()
    remote_key_path = str(getattr(args, "remote_key_path", None) or "").strip()
    remote_known_hosts = str(getattr(args, "remote_known_hosts", "accept_new") or "accept_new").strip().lower()

    has_remote_flag = any(
        bool(str(value or "").strip())
        for value in (
            remote_target,
            remote_dir,
            remote_auth,
            remote_key_path,
        )
    )
    if not has_remote_flag:
        return None, None
    if not remote_target:
        raise SystemExit("--remote-target is required when remote mode is enabled.")
    if not remote_dir:
        raise SystemExit("--remote-dir is required when remote mode is enabled.")
    auth_mode = remote_auth or "key"
    if auth_mode not in {"key", "password"}:
        raise SystemExit("--remote-auth must be one of: key, password.")
    identity_file = remote_key_path
    remote_password: str | None = None
    if auth_mode == "key":
        if not identity_file:
            raise SystemExit("--remote-key-path is required when --remote-auth=key.")
    else:
        try:
            remote_password = getpass.getpass("Remote password: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Remote password input cancelled.") from exc
        if not remote_password:
            raise SystemExit("Remote password is required when --remote-auth=password.")
    try:
        remote_config = normalize_remote_session_config(
            {
                "kind": "remote_ssh",
                "ssh_target": remote_target,
                "remote_dir": remote_dir,
                "auth_mode": auth_mode,
                "identity_file": identity_file,
                "known_hosts_policy": remote_known_hosts,
                "remote_os": "linux",
            }
        )
    except ValueError as exc:
        raise SystemExit(f"Invalid remote config for `{command_name}`: {exc}") from exc
    return remote_config, remote_password


def _maybe_prompt_remote_password_for_session(
    orchestrator: Orchestrator,
    session_id: str,
) -> str | None:
    try:
        session_dir = orchestrator.paths.existing_session_dir(session_id)
    except Exception:
        return None
    remote_config = load_remote_session_config(session_dir)
    if remote_config is None or remote_config.auth_mode != "password":
        return None
    if str(remote_config.password_ref or "").strip():
        stored_password = str(load_remote_session_password(remote_config.password_ref) or "").strip()
        if stored_password:
            return None
    try:
        password = getpass.getpass("Remote password: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SystemExit("Remote password input cancelled.") from exc
    if not password:
        raise SystemExit("Remote password is required for this remote session.")
    return password


def _maybe_apply_staged_changes(orchestrator: Orchestrator, session_id: str) -> None:
    state = orchestrator.project_sync_status(session_id)
    if not state or str(state.get("status", "")) != "pending":
        return
    added = len(state.get("added", []))
    modified = len(state.get("modified", []))
    deleted = len(state.get("deleted", []))
    project_dir = str(state.get("project_dir", ""))
    print(
        (
            "Staged project changes are waiting for confirmation "
            f"(added={added}, modified={modified}, deleted={deleted}) for {project_dir}."
        )
    )
    if _confirm(f"Apply staged changes for session {session_id}?"):
        try:
            result = orchestrator.apply_project_sync(session_id)
        except Exception as exc:
            print(f"Apply failed: {exc}")
            return
        print(
            (
                f"Applied changes: added={result['added']}, "
                f"modified={result['modified']}, deleted={result['deleted']}."
            )
        )
        print(f"You can undo this apply with: opencompany undo {session_id}")
    else:
        print(f"Skipped apply. Run later with: opencompany apply {session_id}")


async def _run_task(
    project_dir: Path,
    app_dir: Path | None,
    locale: str | None,
    task: str,
    debug: bool,
    workspace_mode: str | None = None,
    remote_config: RemoteSessionConfig | None = None,
    remote_password: str | None = None,
    model: str | None = None,
    root_agent_name: str | None = None,
    sandbox_backend: str | None = None,
    preview_chars: int = _RUN_PREVIEW_CHARS_DEFAULT,
) -> None:
    orchestrator = Orchestrator(project_dir, locale=locale, app_dir=app_dir, debug=debug)
    resolved_sandbox_backend = _apply_sandbox_backend(orchestrator, sandbox_backend)
    resolved_model = str(model or "").strip() or None
    resolved_root_agent_name = str(root_agent_name or "").strip() or None
    panel: _RunStatusPanel | None = None
    if _run_status_panel_enabled():
        panel = _RunStatusPanel(
            mode="run",
            locale=orchestrator.locale,
            preview_chars=preview_chars,
        )
        panel.attach(orchestrator)
        await panel.start()
    orchestrator.diagnostics.log(
        component="cli",
        event_type="run_command_started",
        payload={
            "project_dir": str(project_dir),
            "task": task,
            "locale": locale,
            "sandbox_backend": resolved_sandbox_backend,
            "model": resolved_model,
            "root_agent_name": resolved_root_agent_name,
        },
    )
    session = None
    try:
        run_kwargs: dict[str, Any] = {}
        if resolved_model:
            run_kwargs["model"] = resolved_model
        if resolved_root_agent_name:
            run_kwargs["root_agent_name"] = resolved_root_agent_name
        if workspace_mode:
            run_kwargs["workspace_mode"] = workspace_mode
        if remote_config is not None:
            run_kwargs["remote_config"] = remote_config
            if remote_password:
                run_kwargs["remote_password"] = remote_password
        session = await orchestrator.run_task(task, **run_kwargs)
    finally:
        if panel is not None:
            await panel.stop(
                final_session_id=session.id if session is not None else None,
                final_status=session.status.value if session is not None else None,
            )
    if session is None:
        raise RuntimeError("Run finished without session state.")
    orchestrator.diagnostics.log(
        component="cli",
        event_type="run_command_finished",
        session_id=session.id,
        agent_id=session.root_agent_id,
        payload={"status": session.status.value, "completion_state": session.completion_state},
    )
    print(session.final_summary or "Session finished without a summary.")
    print(f"session_id={session.id}")
    print(f"session_status={session.status.value}")
    print(f"completion_state={session.completion_state}")
    _maybe_apply_staged_changes(orchestrator, session.id)


async def _resume(
    app_dir: Path | None,
    locale: str | None,
    session_id: str,
    instruction: str,
    debug: bool,
    model: str | None = None,
    sandbox_backend: str | None = None,
    preview_chars: int = _RUN_PREVIEW_CHARS_DEFAULT,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), locale=locale, app_dir=app_dir, debug=debug)
    resolved_sandbox_backend = _apply_sandbox_backend(orchestrator, sandbox_backend)
    resolved_model = str(model or "").strip() or None
    resumed_session_id = normalized_session_id
    panel: _RunStatusPanel | None = None
    if _run_status_panel_enabled():
        panel = _RunStatusPanel(
            mode="resume",
            locale=orchestrator.locale,
            initial_session_id=resumed_session_id,
            initial_status="resuming",
            preview_chars=preview_chars,
        )
        panel.attach(orchestrator)
        await panel.start()
    orchestrator.diagnostics.log(
        component="cli",
        event_type="resume_command_started",
        session_id=resumed_session_id,
        payload={
            "locale": locale,
            "instruction": instruction,
            "source_session_id": normalized_session_id,
            "sandbox_backend": resolved_sandbox_backend,
            "model": resolved_model,
        },
    )
    session = None
    try:
        remote_password = _maybe_prompt_remote_password_for_session(
            orchestrator,
            resumed_session_id,
        )
        resume_kwargs: dict[str, Any] = {}
        if resolved_model:
            resume_kwargs["model"] = resolved_model
        if remote_password:
            resume_kwargs["remote_password"] = remote_password
        session = await orchestrator.resume(
            resumed_session_id,
            instruction,
            **resume_kwargs,
        )
    finally:
        if panel is not None:
            await panel.stop(
                final_session_id=session.id if session is not None else None,
                final_status=session.status.value if session is not None else None,
            )
    if session is None:
        raise RuntimeError("Resume finished without session state.")
    orchestrator.diagnostics.log(
        component="cli",
        event_type="resume_command_finished",
        session_id=session.id,
        agent_id=session.root_agent_id,
        payload={"status": session.status.value, "completion_state": session.completion_state},
    )
    print(session.final_summary or "Session continued.")
    print(f"session_id={session.id}")
    print(f"session_status={session.status.value}")
    print(f"completion_state={session.completion_state}")
    _maybe_apply_staged_changes(orchestrator, session.id)


def _clone_session(
    app_dir: Path | None,
    locale: str | None,
    session_id: str,
    debug: bool,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), locale=locale, app_dir=app_dir, debug=debug)
    orchestrator.diagnostics.log(
        component="cli",
        event_type="clone_command_started",
        session_id=normalized_session_id,
        payload={
            "locale": locale,
            "source_session_id": normalized_session_id,
        },
    )
    try:
        cloned = orchestrator.clone_session(normalized_session_id)
    except Exception as exc:
        orchestrator.diagnostics.log(
            component="cli",
            event_type="clone_command_failed",
            level="error",
            session_id=normalized_session_id,
            payload={"source_session_id": normalized_session_id},
            error=exc,
        )
        raise
    orchestrator.diagnostics.log(
        component="cli",
        event_type="clone_command_finished",
        session_id=cloned.id,
        payload={
            "source_session_id": normalized_session_id,
            "session_status": cloned.status.value,
        },
    )
    print(f"source_session_id={normalized_session_id}")
    print(f"session_id={cloned.id}")
    print(f"session_status={cloned.status.value}")


def _export(
    app_dir: Path | None,
    session_id: str,
    *,
    export_path: Path | None = None,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    orchestrator.diagnostics.log(
        component="cli",
        event_type="export_command_started",
        session_id=normalized_session_id,
    )
    path = orchestrator.export_logs(normalized_session_id, export_path=export_path)
    orchestrator.diagnostics.log(
        component="cli",
        event_type="export_command_finished",
        session_id=normalized_session_id,
        payload={"export_path": str(path)},
    )
    print(f"session_id={normalized_session_id}")
    print(f"export_path={path}")


def _tool_runs(
    app_dir: Path | None,
    session_id: str,
    *,
    status: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    effective_limit = orchestrator.config.runtime.tools.normalize_list_limit(limit)
    try:
        page = orchestrator.list_tool_runs_page(
            normalized_session_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    runs = page["tool_runs"]
    print(
        json.dumps(
            {
                "session_id": normalized_session_id,
                "count": len(runs),
                "status_filter": status,
                "limit": effective_limit,
                "tool_runs": runs,
                "next_cursor": page.get("next_cursor"),
                "has_more": bool(page.get("has_more", False)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _tool_run_metrics(
    app_dir: Path | None,
    session_id: str,
    *,
    export: bool,
    export_path: Path | None = None,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    if export:
        path = orchestrator.export_tool_run_metrics(
            normalized_session_id,
            export_path=export_path,
        )
        print(path)
        return
    print(
        json.dumps(
            orchestrator.tool_run_metrics(normalized_session_id),
            ensure_ascii=False,
            indent=2,
        )
    )


_EXTRA_EVENT_TYPES = {
    "shell_stream",
    "shell_timeout",
    "tool_timeout",
    "control_message",
    "protocol_error",
    "sandbox_violation",
    "child_summaries_received",
    "agent_completed",
    "agent_summary_requested",
}


def _list_extra_events(
    orchestrator: Orchestrator,
    session_id: str,
    *,
    agent_id: str | None,
    tail: int,
) -> list[dict[str, object]]:
    normalized_agent_id = str(agent_id or "").strip()
    records = orchestrator.load_session_events(session_id)
    extras = [
        record
        for record in records
        if str(record.get("event_type", "")) in _EXTRA_EVENT_TYPES
        and (not normalized_agent_id or str(record.get("agent_id", "")) == normalized_agent_id)
    ]
    if tail > 0:
        return extras[-tail:]
    return extras


def _parse_json_like_text(text: str) -> object | None:
    normalized = text.strip()
    if not normalized:
        return None
    if not (
        (normalized.startswith("{") and normalized.endswith("}"))
        or (normalized.startswith("[") and normalized.endswith("]"))
    ):
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _format_structured_value(value: object, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return fallback
        parsed = _parse_json_like_text(normalized)
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        return normalized
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _format_labeled_structured_value(label: str, value: object, *, fallback: str = "") -> str:
    rendered = _format_structured_value(value, fallback=fallback)
    if not rendered:
        return f"{label}:"
    if "\n" in rendered:
        return f"{label}:\n{rendered}"
    return f"{label}: {rendered}"


def _record_step_count(record: dict[str, object]) -> int | None:
    raw_step = record.get("step_count")
    try:
        step = int(raw_step)
    except (TypeError, ValueError):
        return None
    if step <= 0:
        return None
    return step


def _message_step_number(
    *,
    record: dict[str, object],
    role: str,
    next_step: int,
) -> int:
    derived_step = max(1, next_step) if role == "assistant" else max(1, next_step - 1)
    explicit_step = _record_step_count(record)
    if explicit_step is not None:
        return max(derived_step, explicit_step)
    return derived_step


def _format_message_text(record: dict[str, object], *, step_number: int | None = None) -> str:
    timestamp = str(record.get("timestamp", ""))
    hhmmss = timestamp[11:19] if len(timestamp) >= 19 else "--:--:--"
    agent_id = str(record.get("agent_id", ""))
    agent_name = str(record.get("agent_name", "")).strip() or agent_id
    role = str(record.get("role", "")).strip() or "-"
    message_index = record.get("message_index", "-")
    message = record.get("message")
    if not isinstance(message, dict):
        message = {}
    content = str(message.get("content", "")).strip()
    tool_call_id = str(message.get("tool_call_id", "")).strip()
    if role == "tool":
        lines: list[str] = []
        if tool_call_id:
            lines.append(f"tool_call_id={tool_call_id}")
        if content:
            parsed_content = _parse_json_like_text(content)
            if parsed_content is not None:
                lines.append(_format_labeled_structured_value("content", parsed_content))
            else:
                lines.append(content)
        content = "\n".join(lines).strip()
    else:
        if tool_call_id:
            content = f"tool_call_id={tool_call_id} {content}".strip()
        content = shorten(content.replace(chr(10), " "), width=180, placeholder="...")
    step_label = f" step={step_number}" if step_number is not None else ""
    return (
        f"[{hhmmss}] message {agent_name} ({agent_id[:8]}) "
        f"idx={message_index}{step_label} role={role} "
        f"{content}"
    )


def _format_extra_event_text(record: dict[str, object]) -> str:
    timestamp = str(record.get("timestamp", ""))
    hhmmss = timestamp[11:19] if len(timestamp) >= 19 else "--:--:--"
    event_type = str(record.get("event_type", "")).strip() or "-"
    agent_id = str(record.get("agent_id", ""))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    detail = ""
    if event_type == "shell_stream":
        detail = shorten(str(payload.get("text", "")).replace("\n", "\\n"), width=180, placeholder="...")
    elif event_type in {"tool_run_submitted", "tool_run_updated"}:
        detail = _format_structured_value(payload, fallback="{}")
    elif event_type in {"protocol_error", "sandbox_violation", "control_message"}:
        detail = shorten(str(payload.get("error") or payload.get("content") or ""), width=180, placeholder="...")
    return f"[{hhmmss}] extra {event_type} ({agent_id[:8]}) {detail}".rstrip()


def _messages(
    app_dir: Path | None,
    session_id: str,
    *,
    agent_id: str | None,
    tail: int,
    cursor: str | None,
    include_extra: bool,
    output_format: str,
) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    page = orchestrator.list_session_messages(
        normalized_session_id,
        agent_id=agent_id,
        cursor=cursor,
        limit=500,
        tail=tail,
    )
    messages = page.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    extras: list[dict[str, object]] = []
    if include_extra:
        extras = _list_extra_events(
            orchestrator,
            normalized_session_id,
            agent_id=agent_id,
            tail=max(0, int(tail)),
        )
    if output_format == "text":
        lines: list[str] = []
        next_step_by_agent: dict[str, int] = {}
        for record in messages:
            if isinstance(record, dict):
                current_agent_id = str(record.get("agent_id", "")).strip()
                role = str(record.get("role", "")).strip()
                next_step = max(1, next_step_by_agent.get(current_agent_id, 1))
                step_number = _message_step_number(
                    record=record,
                    role=role,
                    next_step=next_step,
                )
                if role == "assistant":
                    next_step_by_agent[current_agent_id] = max(next_step, step_number + 1)
                else:
                    next_step_by_agent[current_agent_id] = next_step
                lines.append(_format_message_text(record, step_number=step_number))
        for record in extras:
            if isinstance(record, dict):
                lines.append(_format_extra_event_text(record))
        if lines:
            lines.sort()
        print("\n".join(lines) if lines else "(no messages)")
        if page.get("next_cursor"):
            print(f"next_cursor={page.get('next_cursor')}")
        return
    print(
        json.dumps(
            {
                "session_id": normalized_session_id,
                "agent_id": agent_id,
                "messages": messages,
                "next_cursor": page.get("next_cursor"),
                "has_more": bool(page.get("has_more", False)),
                "extra_events": extras if include_extra else [],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _apply(app_dir: Path | None, session_id: str, *, assume_yes: bool) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    state = orchestrator.project_sync_status(normalized_session_id)
    if state is None:
        print(f"No staged project sync found for session {normalized_session_id}.")
        return
    if str(state.get("status", "")) not in {"pending", "reverted"}:
        print(
            "Session "
            f"{normalized_session_id} cannot be applied in status '{state.get('status')}'."
        )
        return
    if not assume_yes and not _confirm(
        f"Apply staged changes for session {normalized_session_id}?"
    ):
        print("Cancelled.")
        return
    try:
        result = orchestrator.apply_project_sync(normalized_session_id)
    except Exception as exc:
        print(f"Apply failed: {exc}")
        return
    print(
        (
            f"Applied changes to {result['project_dir']}: "
            f"added={result['added']}, modified={result['modified']}, deleted={result['deleted']}."
        )
    )
    print(f"Undo command: opencompany undo {normalized_session_id}")


def _undo(app_dir: Path | None, session_id: str, *, assume_yes: bool) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    state = orchestrator.project_sync_status(normalized_session_id)
    if state is None:
        print(f"No project sync state found for session {normalized_session_id}.")
        return
    if str(state.get("status", "")) != "applied":
        print(
            "Session "
            f"{normalized_session_id} cannot be undone in status '{state.get('status')}'."
        )
        return
    if not assume_yes and not _confirm(
        f"Undo applied changes for session {normalized_session_id}?"
    ):
        print("Cancelled.")
        return
    try:
        result = orchestrator.undo_project_sync(normalized_session_id)
    except Exception as exc:
        print(f"Undo failed: {exc}")
        return
    missing = result.get("missing_backups") or []
    print(
        (
            f"Undo completed for {result['project_dir']}: "
            f"removed={result['removed']}, restored={result['restored']}."
        )
    )
    if missing:
        print(f"Missing backups for: {', '.join(str(item) for item in missing)}")


def _terminal(app_dir: Path | None, session_id: str, *, self_check: bool = False) -> None:
    normalized_session_id = _normalize_session_id_or_exit(session_id)
    orchestrator = Orchestrator(Path("."), app_dir=app_dir)
    remote_password = _maybe_prompt_remote_password_for_session(
        orchestrator,
        normalized_session_id,
    )
    if self_check:
        try:
            if remote_password:
                report = asyncio.run(
                    orchestrator.terminal_self_check(
                        normalized_session_id,
                        remote_password=remote_password,
                    )
                )
            else:
                report = asyncio.run(orchestrator.terminal_self_check(normalized_session_id))
        except Exception as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not bool(report.get("passed", False)):
            raise SystemExit("Terminal sandbox self-check failed.")
        return
    try:
        if remote_password:
            opened = orchestrator.open_session_terminal(
                normalized_session_id,
                remote_password=remote_password,
            )
        else:
            opened = orchestrator.open_session_terminal(normalized_session_id)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    workspace_root = str(opened.get("workspace_root", "")).strip()
    if workspace_root:
        print(f"Opened terminal for session {normalized_session_id}: {workspace_root}")
    else:
        print(f"Opened terminal for session {normalized_session_id}.")


def _launch_tui(
    project_dir: Path | None,
    app_dir: Path | None,
    locale: str | None,
    session_id: str | None,
    workspace_mode: str | None,
    remote_config: RemoteSessionConfig | None,
    remote_password: str | None,
    debug: bool,
) -> None:
    resolved_app_dir, diagnostics = _cli_diagnostics(app_dir)
    try:
        from opencompany.tui.app import OpenCompanyApp
    except ImportError as exc:
        raise SystemExit(
            "Textual is required for `opencompany tui`. Install dependencies with `pip install -e .`."
        ) from exc
    diagnostics.log(
        component="cli",
        event_type="tui_launch_started",
        payload={
            "project_dir": str(project_dir) if project_dir is not None else None,
            "session_id": session_id,
            "workspace_mode": workspace_mode,
            "remote_target": remote_config.ssh_target if remote_config is not None else None,
            "remote_dir": remote_config.remote_dir if remote_config is not None else None,
            "remote_auth_mode": remote_config.auth_mode if remote_config is not None else None,
            "locale": locale,
        },
    )
    app = OpenCompanyApp(
        project_dir=project_dir,
        session_id=session_id,
        session_mode=workspace_mode,
        remote_config=remote_config,
        remote_password=remote_password,
        app_dir=resolved_app_dir,
        locale=locale,
        debug=debug,
    )
    try:
        app.run()
        exception = getattr(app, "_exception", None)
        diagnostics.log(
            component="cli",
            event_type="tui_launch_finished",
            level="error" if exception is not None else "info",
            message="TUI returned control to CLI.",
            payload={
                "return_code": app.return_code,
                "exit": getattr(app, "_exit", False),
                "closed": getattr(app, "_closed", False),
                "closing": getattr(app, "_closing", False),
                "exception_type": exception.__class__.__name__ if exception is not None else None,
            },
            error=exception,
        )
    except BaseException as exc:
        diagnostics.log(
            component="cli",
            event_type="tui_launch_failed",
            level="error",
            error=exc,
        )
        raise


def _launch_ui(
    project_dir: Path | None,
    app_dir: Path | None,
    locale: str | None,
    session_id: str | None,
    workspace_mode: str | None,
    remote_config: RemoteSessionConfig | None,
    remote_password: str | None,
    debug: bool,
    host: str,
    port: int,
) -> None:
    resolved_app_dir, diagnostics = _cli_diagnostics(app_dir)
    try:
        import uvicorn

        from opencompany.webui import create_webui_app
    except ImportError as exc:
        raise SystemExit(
            "FastAPI and Uvicorn are required for `opencompany ui`. Install dependencies with `pip install -e .`."
        ) from exc

    diagnostics.log(
        component="cli",
        event_type="ui_launch_started",
        payload={
            "project_dir": str(project_dir) if project_dir is not None else None,
            "session_id": session_id,
            "workspace_mode": workspace_mode,
            "remote_target": remote_config.ssh_target if remote_config is not None else None,
            "remote_dir": remote_config.remote_dir if remote_config is not None else None,
            "remote_auth_mode": remote_config.auth_mode if remote_config is not None else None,
            "locale": locale,
            "host": host,
            "port": port,
        },
    )
    app = create_webui_app(
        project_dir=project_dir,
        session_id=session_id,
        session_mode=workspace_mode,
        remote_config=remote_config,
        remote_password=remote_password,
        app_dir=resolved_app_dir,
        locale=locale,
        debug=debug,
    )
    display_host = host
    if host in {"0.0.0.0", "::"}:
        display_host = "127.0.0.1"
    print(f"OpenCompany Web UI listening at http://{display_host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
        diagnostics.log(
            component="cli",
            event_type="ui_launch_finished",
            payload={
                "host": host,
                "port": port,
            },
        )
    except BaseException as exc:
        diagnostics.log(
            component="cli",
            event_type="ui_launch_failed",
            level="error",
            error=exc,
            payload={"host": host, "port": port},
        )
        raise


def main() -> None:
    args = build_parser().parse_args()
    project_dir_arg = getattr(args, "project_dir", None)
    project_dir = Path(project_dir_arg).resolve() if project_dir_arg else None
    app_dir = (
        Path(args.app_dir).resolve()
        if getattr(args, "app_dir", None)
        else None
    )
    workspace_mode = getattr(args, "workspace_mode", None)
    try:
        if args.command == "run":
            remote_config, remote_password = _remote_cli_config_from_args(
                args,
                command_name="run",
            )
            if remote_config is not None and workspace_mode == WorkspaceMode.STAGED.value:
                raise SystemExit("Remote mode is not supported with --workspace-mode staged.")
            asyncio.run(
                _run_task(
                    project_dir,
                    app_dir,
                    args.locale,
                    args.task,
                    bool(args.debug),
                    workspace_mode=workspace_mode,
                    remote_config=remote_config,
                    remote_password=remote_password,
                    model=getattr(args, "model", None),
                    root_agent_name=getattr(args, "root_agent_name", None),
                    sandbox_backend=getattr(args, "sandbox_backend", None),
                    preview_chars=int(getattr(args, "preview_chars", _RUN_PREVIEW_CHARS_DEFAULT)),
                )
            )
        elif args.command == "resume":
            asyncio.run(
                _resume(
                    app_dir,
                    args.locale,
                    args.session_id,
                    args.instruction,
                    bool(args.debug),
                    model=getattr(args, "model", None),
                    sandbox_backend=getattr(args, "sandbox_backend", None),
                    preview_chars=int(
                        getattr(args, "preview_chars", _RUN_PREVIEW_CHARS_DEFAULT)
                    ),
                )
            )
        elif args.command == "clone":
            _clone_session(
                app_dir,
                args.locale,
                args.session_id,
                bool(args.debug),
            )
        elif args.command == "export-logs":
            _export(
                app_dir,
                args.session_id,
                export_path=_resolve_export_file_path(
                    getattr(args, "export_path", None),
                    flag_name="--export-path",
                ),
            )
        elif args.command == "messages":
            _messages(
                app_dir,
                args.session_id,
                agent_id=getattr(args, "agent_id", None),
                tail=int(getattr(args, "tail", 200)),
                cursor=getattr(args, "cursor", None),
                include_extra=bool(getattr(args, "include_extra", False)),
                output_format=str(getattr(args, "format", "json")),
            )
        elif args.command == "tool-runs":
            _tool_runs(
                app_dir,
                args.session_id,
                status=args.status,
                limit=args.limit,
                cursor=getattr(args, "cursor", None),
            )
        elif args.command == "tool-run-metrics":
            _tool_run_metrics(
                app_dir,
                args.session_id,
                export=bool(getattr(args, "export", False)),
                export_path=_resolve_export_file_path(
                    getattr(args, "export_path", None),
                    flag_name="--export-path",
                ),
            )
        elif args.command == "apply":
            _apply(app_dir, args.session_id, assume_yes=bool(args.yes))
        elif args.command == "undo":
            _undo(app_dir, args.session_id, assume_yes=bool(args.yes))
        elif args.command == "terminal":
            _terminal(
                app_dir,
                args.session_id,
                self_check=bool(getattr(args, "self_check", False)),
            )
        elif args.command == "tui":
            remote_config, remote_password = _remote_cli_config_from_args(
                args,
                command_name="tui",
            )
            if getattr(args, "session_id", None) and workspace_mode:
                raise SystemExit(
                    "--workspace-mode can only be set when creating a new session, not with --session-id."
                )
            if getattr(args, "session_id", None) and remote_config is not None:
                raise SystemExit(
                    "Remote flags can only be set when creating a new session, not with --session-id."
                )
            if remote_config is not None and workspace_mode == WorkspaceMode.STAGED.value:
                raise SystemExit("Remote mode is not supported with --workspace-mode staged.")
            _launch_tui(
                project_dir,
                app_dir,
                args.locale,
                (
                    _normalize_session_id_or_exit(args.session_id)
                    if getattr(args, "session_id", None)
                    else None
                ),
                workspace_mode,
                remote_config,
                remote_password,
                bool(args.debug),
            )
        elif args.command == "ui":
            remote_config, remote_password = _remote_cli_config_from_args(
                args,
                command_name="ui",
            )
            if getattr(args, "session_id", None) and workspace_mode:
                raise SystemExit(
                    "--workspace-mode can only be set when creating a new session, not with --session-id."
                )
            if getattr(args, "session_id", None) and remote_config is not None:
                raise SystemExit(
                    "Remote flags can only be set when creating a new session, not with --session-id."
                )
            if remote_config is not None and workspace_mode == WorkspaceMode.STAGED.value:
                raise SystemExit("Remote mode is not supported with --workspace-mode staged.")
            _launch_ui(
                project_dir,
                app_dir,
                args.locale,
                (
                    _normalize_session_id_or_exit(args.session_id)
                    if getattr(args, "session_id", None)
                    else None
                ),
                workspace_mode,
                remote_config,
                remote_password,
                bool(args.debug),
                str(args.host),
                int(args.port),
            )
    except KeyboardInterrupt:
        print("Interrupted.")
