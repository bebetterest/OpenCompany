from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from opencompany.models import ShellCommandRequest, ShellCommandResult


class SandboxError(RuntimeError):
    pass


ShellEventCallback = Callable[[str, str], Awaitable[None] | None]


class SandboxBackend(ABC):
    @abstractmethod
    def resolve_cli_path(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_settings(self, request: ShellCommandRequest) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def run_command(
        self,
        request: ShellCommandRequest,
        on_event: ShellEventCallback | None = None,
    ) -> ShellCommandResult:
        raise NotImplementedError

    def should_block_outside_workspace_write(self) -> bool:
        return True

    def build_terminal_command(
        self,
        request: ShellCommandRequest,
        *,
        settings_path: Path,
        remote_settings_path: str | None = None,
    ) -> str | None:
        del request, settings_path, remote_settings_path
        return None

    def cleanup_session(self, _session_id: str) -> None:
        return None
