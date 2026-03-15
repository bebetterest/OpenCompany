from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from opencompany.config import SandboxConfig
from opencompany.models import RemoteShellContext, ShellCommandRequest, ShellCommandResult
from opencompany.remote import parse_ssh_target
from opencompany.sandbox.base import SandboxBackend, SandboxError, ShellEventCallback
from opencompany.utils import resolve_in_workspace, truncate_text


class NoSandboxBackend(SandboxBackend):
    PROCESS_KILL_TIMEOUT_SECONDS = 5.0
    STREAM_DRAIN_TIMEOUT_SECONDS = 1.0

    def __init__(self, config: SandboxConfig, app_dir: Path) -> None:
        self.config = config
        self.app_dir = app_dir
        self._remote_control_paths: dict[str, Path] = {}
        self._remote_contexts: dict[str, RemoteShellContext] = {}

    def resolve_cli_path(self) -> str:
        if self.config.cli_path:
            return str(self.config.cli_path)
        return shutil.which("bash") or "/bin/bash"

    def build_settings(self, request: ShellCommandRequest) -> dict:
        del request
        return {}

    def should_block_outside_workspace_write(self) -> bool:
        return False

    def build_terminal_command(
        self,
        request: ShellCommandRequest,
        *,
        settings_path: Path,
        remote_settings_path: str | None = None,
    ) -> str | None:
        del settings_path, remote_settings_path
        if request.remote is None:
            return "/bin/bash --noprofile --norc -i"
        workspace_root = str(request.workspace_root)
        return (
            f"workspace_root={shlex.quote(workspace_root)}; "
            "resolved_workspace_root=\"$workspace_root\"; "
            "if [ -d \"$workspace_root\" ]; then resolved_workspace_root=$(cd \"$workspace_root\" && /bin/pwd -P); fi; "
            "cd \"$resolved_workspace_root\"; "
            "exec /bin/bash --noprofile --norc -i"
        )

    async def run_command(
        self,
        request: ShellCommandRequest,
        on_event: ShellEventCallback | None = None,
    ) -> ShellCommandResult:
        if request.remote is not None:
            return await self._run_remote_command(request, on_event=on_event)
        resolve_in_workspace(
            request.workspace_root,
            str(request.cwd.relative_to(request.workspace_root)),
        )
        env = os.environ.copy()
        env.update(request.environment)
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = -1
        timed_out = False
        killed = False
        termination_reason: str | None = None
        reader_tasks_cancelled = False

        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            request.command,
            cwd=str(request.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name != "nt",
        )
        assert process.stdout is not None
        assert process.stderr is not None

        async def _read_stream(stream, channel: str, sink: list[str]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                sink.append(text)
                if on_event:
                    maybe = on_event(channel, text)
                    if asyncio.iscoroutine(maybe):
                        await maybe

        stdout_task = asyncio.create_task(_read_stream(process.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(_read_stream(process.stderr, "stderr", stderr_parts))
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            killed = True
            termination_reason = await self._terminate_process(process)
        except asyncio.CancelledError:
            killed = True
            termination_reason = await self._terminate_process(process)
            raise
        finally:
            reader_tasks_cancelled = await self._drain_stream_tasks(stdout_task, stderr_task)
        exit_code = process.returncode if process.returncode is not None else -1

        if timed_out:
            stderr_parts.append(
                self._diagnostic_line(
                    f"Command timed out after {request.timeout_seconds}s and was force-terminated"
                    f" ({termination_reason or 'termination_requested'})."
                )
            )
        elif reader_tasks_cancelled:
            stderr_parts.append(
                self._diagnostic_line(
                    "Shell stream drain exceeded the post-exit grace period; returning partial output."
                )
            )

        duration_ms = int((loop.time() - started_at) * 1000)
        return ShellCommandResult(
            exit_code=exit_code,
            stdout=truncate_text("".join(stdout_parts)),
            stderr=truncate_text("".join(stderr_parts)),
            command=request.command,
            timed_out=timed_out,
            duration_ms=duration_ms,
            timeout_seconds=request.timeout_seconds,
            killed=killed,
            termination_reason=termination_reason,
            reader_tasks_cancelled=reader_tasks_cancelled,
        )

    def cleanup_session(self, session_id: str) -> None:
        normalized = str(session_id or "").strip()
        if not normalized:
            return
        keys = [key for key in self._remote_control_paths if key.startswith(f"{normalized}::")]
        for key in keys:
            self._remote_contexts.pop(key, None)
            control_path = self._remote_control_paths.pop(key, None)
            if control_path is not None:
                self._close_controlmaster_socket(key, control_path)

    async def _run_remote_command(
        self,
        request: ShellCommandRequest,
        on_event: ShellEventCallback | None = None,
    ) -> ShellCommandResult:
        remote = request.remote
        if remote is None:
            raise SandboxError("Remote shell context is missing.")
        if remote.config.remote_os != "linux":
            raise SandboxError("Only remote Linux hosts are supported in V1.")
        if remote.config.auth_mode == "password" and not str(remote.password or "").strip():
            raise SandboxError("Password auth selected but remote_password was not provided.")
        cache_key = self._remote_cache_key(remote)
        self._remote_contexts[cache_key] = remote
        control_path = self._remote_control_path_for_key(cache_key)
        remote_exec = (
            "set -euo pipefail; "
            f"cd {shlex.quote(str(request.cwd))}; "
            f"exec /bin/bash --noprofile --norc -c {shlex.quote(request.command)}"
        )
        return await self._run_ssh_command(
            remote=remote,
            control_path=control_path,
            command=remote_exec,
            timeout_seconds=float(request.timeout_seconds),
            on_event=on_event,
        )

    async def _run_ssh_command(
        self,
        *,
        remote: RemoteShellContext,
        control_path: Path,
        command: str,
        timeout_seconds: float,
        on_event: ShellEventCallback | None,
    ) -> ShellCommandResult:
        ssh_command, password_file = self._build_ssh_command(
            remote=remote,
            control_path=control_path,
            command=command,
        )
        env = os.environ.copy()
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = -1
        timed_out = False
        killed = False
        termination_reason: str | None = None
        reader_tasks_cancelled = False
        try:
            process = await asyncio.create_subprocess_exec(
                *ssh_command,
                cwd=str(self.app_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=os.name != "nt",
            )
            assert process.stdout is not None
            assert process.stderr is not None

            async def _read_stream(stream, channel: str, sink: list[str]) -> None:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    sink.append(text)
                    if on_event:
                        maybe = on_event(channel, text)
                        if asyncio.iscoroutine(maybe):
                            await maybe

            stdout_task = asyncio.create_task(_read_stream(process.stdout, "stdout", stdout_parts))
            stderr_task = asyncio.create_task(_read_stream(process.stderr, "stderr", stderr_parts))
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                killed = True
                termination_reason = await self._terminate_process(process)
            except asyncio.CancelledError:
                killed = True
                termination_reason = await self._terminate_process(process)
                raise
            finally:
                reader_tasks_cancelled = await self._drain_stream_tasks(stdout_task, stderr_task)
            exit_code = process.returncode if process.returncode is not None else -1
        finally:
            if password_file is not None:
                password_file.unlink(missing_ok=True)

        if timed_out:
            stderr_parts.append(
                self._diagnostic_line(
                    f"Command timed out after {timeout_seconds}s and was force-terminated"
                    f" ({termination_reason or 'termination_requested'})."
                )
            )
        elif reader_tasks_cancelled:
            stderr_parts.append(
                self._diagnostic_line(
                    "Shell stream drain exceeded the post-exit grace period; returning partial output."
                )
            )
        duration_ms = int((loop.time() - started_at) * 1000)
        return ShellCommandResult(
            exit_code=exit_code,
            stdout=truncate_text("".join(stdout_parts)),
            stderr=truncate_text("".join(stderr_parts)),
            command=command,
            timed_out=timed_out,
            duration_ms=duration_ms,
            timeout_seconds=timeout_seconds,
            killed=killed,
            termination_reason=termination_reason,
            reader_tasks_cancelled=reader_tasks_cancelled,
        )

    def _build_ssh_command(
        self,
        *,
        remote: RemoteShellContext,
        control_path: Path,
        command: str,
    ) -> tuple[list[str], Path | None]:
        user, host, port = parse_ssh_target(remote.config.ssh_target)
        destination = f"{user}@{host}"
        options = [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=600",
            "-o",
            f"ControlPath={str(control_path)}",
            "-o",
            f"StrictHostKeyChecking={self._known_hosts_option(remote)}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if port is not None:
            options.extend(["-p", str(port)])
        password_file: Path | None = None
        if remote.config.auth_mode == "key":
            options.extend(["-i", str(Path(remote.config.identity_file).expanduser())])
            options.extend(["-o", "BatchMode=yes"])
            command_list = ["ssh", *options, destination, command]
            return command_list, None
        if not str(remote.password or "").strip():
            raise SandboxError("Remote password is required.")
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise SandboxError(
                "sshpass is required for password auth. Install sshpass locally or use key auth."
            )
        password_file = Path(tempfile.mkstemp(prefix="opencompany_sshpass_", text=True)[1])
        password_file.write_text(remote.password, encoding="utf-8")
        options.extend(["-o", "BatchMode=no"])
        command_list = [sshpass, "-f", str(password_file), "ssh", *options, destination, command]
        return command_list, password_file

    def _close_controlmaster_socket(self, cache_key: str, control_path: Path) -> None:
        ssh_target = cache_key.split("::", 1)[1]
        destination = ssh_target
        port: int | None = None
        with contextlib.suppress(Exception):
            user, host, port = parse_ssh_target(ssh_target)
            destination = f"{user}@{host}"
        command = [
            "ssh",
            "-o",
            f"ControlPath={str(control_path)}",
        ]
        if port is not None:
            command.extend(["-p", str(port)])
        command.extend(["-O", "exit", destination])
        with contextlib.suppress(Exception):
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        control_path.unlink(missing_ok=True)

    def _remote_control_path_for_key(self, cache_key: str) -> Path:
        cached = self._remote_control_paths.get(cache_key)
        if cached is not None:
            return cached
        base = self._remote_control_runtime_dir()
        base.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:16]
        control_path = base / f"cm-{digest}"
        self._remote_control_paths[cache_key] = control_path
        return control_path

    @staticmethod
    def _remote_control_runtime_dir() -> Path:
        if os.name == "nt":
            return Path(tempfile.gettempdir()) / "opencompany_ssh"
        return Path("/tmp/opencompany-ssh")

    @staticmethod
    def _remote_cache_key(remote: RemoteShellContext) -> str:
        return f"{remote.session_id}::{remote.config.ssh_target}"

    @staticmethod
    def _known_hosts_option(remote: RemoteShellContext) -> str:
        if remote.config.known_hosts_policy == "strict":
            return "yes"
        return "accept-new"

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> str:
        if process.returncode is not None:
            return "process_exited_before_termination"

        used_process_group = False
        if os.name != "nt":
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
                used_process_group = True

        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        try:
            await asyncio.wait_for(process.wait(), timeout=self.PROCESS_KILL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return "kill_requested_wait_timeout"

        if used_process_group:
            return "process_group_killed"
        return "process_killed"

    async def _drain_stream_tasks(self, *tasks: asyncio.Task[None]) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.STREAM_DRAIN_TIMEOUT_SECONDS,
            )
            return False
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return True

    @staticmethod
    def _diagnostic_line(message: str) -> str:
        return f"{message}\n"
