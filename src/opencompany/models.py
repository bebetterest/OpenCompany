from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AgentRole(str, Enum):
    ROOT = "root"
    WORKER = "worker"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TERMINATED = "terminated"


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class WorkspaceMode(str, Enum):
    STAGED = "staged"
    DIRECT = "direct"


class ToolRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SteerRunStatus(str, Enum):
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class RemoteSessionConfig:
    kind: str = "remote_ssh"
    ssh_target: str = ""
    remote_dir: str = ""
    auth_mode: str = "key"
    identity_file: str = ""
    known_hosts_policy: str = "accept_new"
    remote_os: str = "linux"
    password_ref: str = ""


@dataclass(slots=True)
class RemoteShellContext:
    session_id: str
    config: RemoteSessionConfig
    password: str = ""


@dataclass(slots=True)
class WorkspaceRef:
    id: str
    path: Path
    base_snapshot_path: Path
    parent_workspace_id: str | None
    readonly: bool = False


@dataclass(slots=True)
class AgentNode:
    id: str
    session_id: str
    name: str
    role: AgentRole
    instruction: str
    workspace_id: str
    parent_agent_id: str | None = None
    status: AgentStatus = AgentStatus.PENDING
    status_reason: str | None = None
    children: list[str] = field(default_factory=list)
    summary: str | None = None
    next_recommendation: str | None = None
    diff_artifact: str | None = None
    completion_status: str | None = None
    step_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RunSession:
    id: str
    project_dir: Path
    task: str
    locale: str
    root_agent_id: str
    workspace_mode: WorkspaceMode = WorkspaceMode.STAGED
    status: SessionStatus = SessionStatus.RUNNING
    status_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""
    loop_index: int = 0
    final_summary: str | None = None
    completion_state: str | None = None
    follow_up_needed: bool = False
    enabled_skill_ids: list[str] = field(default_factory=list)
    skill_bundle_root: str = ""
    skills_state: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EventRecord:
    timestamp: str
    session_id: str
    agent_id: str | None
    parent_agent_id: str | None
    event_type: str
    phase: str
    payload: dict[str, Any]
    workspace_id: str | None
    checkpoint_seq: int = 0


@dataclass(slots=True)
class CheckpointState:
    session: dict[str, Any]
    agents: dict[str, dict[str, Any]]
    workspaces: dict[str, dict[str, Any]]
    pending_agent_ids: list[str]
    pending_tool_run_ids: list[str]
    root_loop: int
    interrupted: bool = False


@dataclass(slots=True)
class ShellCommandRequest:
    command: str
    cwd: Path
    workspace_root: Path
    writable_paths: list[Path]
    timeout_seconds: float
    network_policy: str = "deny_all"
    allowed_domains: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    remote: RemoteShellContext | None = None


@dataclass(slots=True)
class ShellCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    command: str
    timed_out: bool = False
    duration_ms: int = 0
    timeout_seconds: int | float | None = None
    killed: bool = False
    termination_reason: str | None = None
    reader_tasks_cancelled: bool = False


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(slots=True)
class WorkerCompletion:
    summary: str
    status: str
    next_recommendation: str
    diff_artifact: str


@dataclass(slots=True)
class RootFinalization:
    user_summary: str
    completion_state: str
    follow_up_needed: bool


@dataclass(slots=True)
class ToolRun:
    id: str
    session_id: str
    agent_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: ToolRunStatus
    blocking: bool
    status_reason: str | None = None
    parent_run_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(slots=True)
class SteerRun:
    id: str
    session_id: str
    agent_id: str
    content: str
    source: str
    status: SteerRunStatus
    source_agent_id: str = "user"
    source_agent_name: str = "user"
    status_reason: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    cancelled_at: str | None = None
    delivered_step: int | None = None


def normalize_workspace_mode(value: WorkspaceMode | str | None) -> WorkspaceMode:
    if isinstance(value, WorkspaceMode):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return WorkspaceMode(normalized)
    except ValueError:
        return WorkspaceMode.STAGED
