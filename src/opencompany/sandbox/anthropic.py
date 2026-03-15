from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import posixpath
import shlex
import shutil
import signal
import sys
import tempfile
from pathlib import Path, PurePosixPath

from opencompany.config import SandboxConfig
from opencompany.models import RemoteShellContext, ShellCommandRequest, ShellCommandResult
from opencompany.remote import parse_ssh_target
from opencompany.sandbox.base import SandboxBackend, SandboxError, ShellEventCallback
from opencompany.utils import resolve_in_workspace, truncate_text


class AnthropicSandboxBackend(SandboxBackend):
    PROCESS_KILL_TIMEOUT_SECONDS = 5.0
    STREAM_DRAIN_TIMEOUT_SECONDS = 1.0
    REMOTE_DEPENDENCY_TIMEOUT_SECONDS = 600.0

    def __init__(self, config: SandboxConfig, app_dir: Path) -> None:
        self.config = config
        self.app_dir = app_dir
        self._resolved_cli_path: str | None = None
        self._resolved_ripgrep_command: str | None = None
        self._remote_control_paths: dict[str, Path] = {}
        self._remote_dependency_checked: set[str] = set()
        self._remote_settings_hash: dict[str, str] = {}
        self._remote_contexts: dict[str, RemoteShellContext] = {}
        self._remote_gc_checked: set[str] = set()
        self._remote_resolved_paths: dict[str, dict[str, str]] = {}

    def resolve_cli_path(self) -> str:
        if self._resolved_cli_path:
            return self._resolved_cli_path
        if self.config.cli_path:
            self._resolved_cli_path = self.config.cli_path
            return self._resolved_cli_path
        local = self.app_dir / "node_modules" / ".bin" / "srt"
        if local.exists():
            self._resolved_cli_path = str(local)
            return self._resolved_cli_path
        system = shutil.which("srt")
        if system:
            self._resolved_cli_path = system
            return self._resolved_cli_path
        raise SandboxError(
            "Anthropic sandbox runtime not found. Run `npm install` or set sandbox.cli_path."
        )

    def resolve_ripgrep_command(self) -> str:
        if self._resolved_ripgrep_command:
            return self._resolved_ripgrep_command
        candidates: list[str | None] = [
            shutil.which("rg"),
            str((Path(sys.executable).resolve().parent / "rg")),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.exists() and path.is_file():
                self._resolved_ripgrep_command = str(path.resolve())
                return self._resolved_ripgrep_command
        raise SandboxError(
            "ripgrep (rg) is required for the Anthropic sandbox runtime. Install it in the active environment."
        )

    def build_settings(self, request: ShellCommandRequest) -> dict:
        if request.remote is not None:
            writable_paths: list[str] = []
            for path in request.writable_paths:
                resolved = self._normalize_remote_posix_path(str(path))
                writable_paths.append(str(resolved))
            # Keep a cwd-relative writable anchor for hosts where absolute bind
            # path resolution differs under remote bwrap setup.
            writable_paths.append(".")
            writable_paths.append("/tmp")
            policy = str(request.network_policy or "").strip().lower()
            if not policy:
                policy = "deny_all"
            if policy not in {"deny_all", "allow_all", "allowlist"}:
                raise SandboxError(f"Unsupported sandbox network policy: {policy}")
            if policy == "deny_all":
                allowed_domains: list[str] = []
            elif policy == "allow_all":
                allowed_domains = ["*"]
            else:
                allowed_domains = [
                    str(domain).strip()
                    for domain in request.allowed_domains
                    if str(domain).strip()
                ]
                if not allowed_domains:
                    raise SandboxError(
                        "sandbox.network_policy='allowlist' requires non-empty allowed_domains."
                    )
            network_settings: dict[str, object] = {
                "allowedDomains": allowed_domains,
                "deniedDomains": [],
                "allowLocalBinding": False,
            }
            if policy == "deny_all":
                network_settings["httpProxyPort"] = 65535
                network_settings["socksProxyPort"] = 65534
            return {
                "network": network_settings,
                "filesystem": {
                    "denyRead": ["~/.ssh", "~/.aws", "~/.config"],
                    "allowWrite": sorted(set(writable_paths)),
                    "denyWrite": [".env", ".git/config", ".git/hooks"],
                },
                "ripgrep": {
                    "command": "rg",
                },
            }
        writable_paths = []
        for path in request.writable_paths:
            resolved = path.resolve()
            if resolved != request.workspace_root.resolve() and request.workspace_root.resolve() not in resolved.parents:
                raise SandboxError(f"Writable path escapes workspace: {resolved}")
            writable_paths.append(str(resolved))
        writable_paths.append("/tmp")
        policy = str(request.network_policy or "").strip().lower()
        if not policy:
            policy = "deny_all"
        if policy not in {"deny_all", "allow_all", "allowlist"}:
            raise SandboxError(f"Unsupported sandbox network policy: {policy}")
        if policy == "deny_all":
            allowed_domains: list[str] = []
        elif policy == "allow_all":
            allowed_domains = ["*"]
        else:
            allowed_domains = [str(domain).strip() for domain in request.allowed_domains if str(domain).strip()]
            if not allowed_domains:
                raise SandboxError(
                    "sandbox.network_policy='allowlist' requires non-empty allowed_domains."
                )
        network_settings: dict[str, object] = {
            "allowedDomains": allowed_domains,
            "deniedDomains": [],
            "allowLocalBinding": False,
        }
        if policy == "deny_all":
            # srt 0.0.16 CLI initializes local proxy listeners even when no domains are allowed.
            # Provide inert external proxy ports so it skips local listen() startup paths.
            network_settings["httpProxyPort"] = 65535
            network_settings["socksProxyPort"] = 65534
        return {
            "network": network_settings,
            "filesystem": {
                "denyRead": ["~/.ssh", "~/.aws", "~/.config"],
                "allowWrite": sorted(set(writable_paths)),
                "denyWrite": [".env", ".git/config", ".git/hooks"],
            },
            "ripgrep": {
                "command": self.resolve_ripgrep_command(),
            },
        }

    @staticmethod
    def _normalize_remote_posix_path(raw_path: str) -> PurePosixPath:
        normalized = str(raw_path or "").strip()
        if not normalized:
            raise SandboxError("Remote path is required.")
        candidate = PurePosixPath(posixpath.normpath(normalized))
        if not candidate.is_absolute():
            raise SandboxError(f"Remote path must be absolute: {normalized}")
        return candidate

    @staticmethod
    def build_sandbox_command(command: str) -> str:
        # Encode the user command before handing it to `srt` so outer quote layers
        # (notably shell-quote in sandbox-runtime) cannot rewrite literal `!`.
        encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
        decode_and_exec = (
            f"__OC_COMMAND_B64={shlex.quote(encoded_command)}; "
            "__OC_COMMAND=\"$(printf %s \"$__OC_COMMAND_B64\" | base64 --decode 2>/dev/null || "
            "printf %s \"$__OC_COMMAND_B64\" | base64 -d 2>/dev/null || "
            "printf %s \"$__OC_COMMAND_B64\" | base64 -D)\" || exit 127; "
            "/bin/bash --noprofile --norc -c \"$__OC_COMMAND\""
        )
        return shlex.join(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                decode_and_exec,
            ]
        )

    async def run_command(
        self,
        request: ShellCommandRequest,
        on_event: ShellEventCallback | None = None,
    ) -> ShellCommandResult:
        if request.remote is not None:
            return await self._run_remote_command(request, on_event=on_event)
        cli_path = self.resolve_cli_path()
        resolve_in_workspace(request.workspace_root, str(request.cwd.relative_to(request.workspace_root)))
        settings = self.build_settings(request)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(settings, handle)
            handle.flush()
            settings_path = handle.name

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

        try:
            sandbox_command = self.build_sandbox_command(request.command)
            process = await asyncio.create_subprocess_exec(
                cli_path,
                "--settings",
                settings_path,
                sandbox_command,
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
                reader_tasks_cancelled = await self._drain_stream_tasks(
                    stdout_task,
                    stderr_task,
                )

            exit_code = process.returncode if process.returncode is not None else -1
        finally:
            Path(settings_path).unlink(missing_ok=True)

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
            remote_context = self._remote_contexts.pop(key, None)
            control_path = self._remote_control_paths.pop(key, None)
            if remote_context is not None and control_path is not None:
                self._best_effort_remote_cache_cleanup(remote_context, control_path)
            if control_path is not None:
                self._close_controlmaster_socket(key, control_path)
            self._remote_dependency_checked.discard(key)
            self._remote_settings_hash.pop(key, None)
            self._remote_gc_checked.discard(key)
            self._remote_resolved_paths.pop(key, None)

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
        runtime_request = await self._prepare_remote_runtime_request(
            cache_key=cache_key,
            request=request,
            remote=remote,
            control_path=control_path,
        )
        settings = self.build_settings(runtime_request)
        settings_json = json.dumps(settings, ensure_ascii=True, separators=(",", ":"))
        settings_hash = hashlib.sha256(settings_json.encode("utf-8")).hexdigest()
        remote_cache_dir = self._remote_session_cache_dir(remote.session_id)
        remote_settings_path = f"{remote_cache_dir}/settings.json"
        remote_hash_path = f"{remote_cache_dir}/settings.sha256"

        if cache_key not in self._remote_gc_checked:
            with contextlib.suppress(Exception):
                await self._run_ssh_command(
                    remote=remote,
                    control_path=control_path,
                    command=self._remote_cleanup_script(remote.session_id),
                    timeout_seconds=max(10.0, float(request.timeout_seconds)),
                    on_event=None,
                )
            self._remote_gc_checked.add(cache_key)

        if cache_key not in self._remote_dependency_checked:
            dependency_timeout = max(
                self.REMOTE_DEPENDENCY_TIMEOUT_SECONDS,
                float(request.timeout_seconds),
            )
            dependency_result = await self._run_ssh_command(
                remote=remote,
                control_path=control_path,
                command=self.remote_dependency_guard_script(remote.session_id),
                timeout_seconds=dependency_timeout,
                on_event=on_event,
            )
            if dependency_result.exit_code != 0:
                raise SandboxError(
                    "Remote sandbox dependency check failed. "
                    "Ensure remote host provides Node.js >= 18, npm, rg, bubblewrap (bwrap), and socat, "
                    "and allows unprivileged user namespaces for bubblewrap "
                    "(runtime may attempt privileged auto-install).\n"
                    + dependency_result.stderr.strip()
                )
            self._remote_dependency_checked.add(cache_key)

        if self._remote_settings_hash.get(cache_key) != settings_hash:
            ensure_script = (
                f"set -euo pipefail; "
                f"remote_cache_dir={remote_cache_dir}; "
                f"remote_settings_path={remote_settings_path}; "
                f"remote_hash_path={remote_hash_path}; "
                f"mkdir -p \"$remote_cache_dir\"; "
                f"current=''; "
                f"if [ -f \"$remote_hash_path\" ]; then "
                f"current=$(cat \"$remote_hash_path\" 2>/dev/null || true); "
                f"fi; "
                f"if [ \"$current\" != {shlex.quote(settings_hash)} ]; then "
                f"cat > \"$remote_settings_path\".tmp && "
                f"mv \"$remote_settings_path\".tmp \"$remote_settings_path\" && "
                f"printf %s {shlex.quote(settings_hash)} > \"$remote_hash_path\"; "
                f"fi"
            )
            ensure_result = await self._run_ssh_command(
                remote=remote,
                control_path=control_path,
                command=ensure_script,
                timeout_seconds=max(20.0, float(request.timeout_seconds)),
                stdin_text=settings_json,
                on_event=None,
            )
            if ensure_result.exit_code != 0:
                raise SandboxError(
                    "Failed to prepare remote sandbox settings.\n" + ensure_result.stderr.strip()
                )
            self._remote_settings_hash[cache_key] = settings_hash

        sandbox_command = self.build_sandbox_command(runtime_request.command)
        remote_exec = (
            "set -euo pipefail; "
            "PATH=\"$HOME/.local/bin:$HOME/.npm/bin:$PATH\"; "
            f"cd {shlex.quote(str(runtime_request.cwd))}; "
            f"exec srt --settings {remote_settings_path} {sandbox_command}"
        )
        return await self._run_ssh_command(
            remote=remote,
            control_path=control_path,
            command=remote_exec,
            timeout_seconds=float(runtime_request.timeout_seconds),
            on_event=on_event,
        )

    async def _prepare_remote_runtime_request(
        self,
        *,
        cache_key: str,
        request: ShellCommandRequest,
        remote: RemoteShellContext,
        control_path: Path,
    ) -> ShellCommandRequest:
        requested_root = str(self._normalize_remote_posix_path(str(request.workspace_root)))
        requested_cwd = str(self._normalize_remote_posix_path(str(request.cwd)))
        requested_writable = [
            str(self._normalize_remote_posix_path(str(path)))
            for path in request.writable_paths
        ]
        requested_paths = [requested_root, requested_cwd, *requested_writable]
        resolved_map = await self._resolve_remote_runtime_paths(
            cache_key=cache_key,
            remote=remote,
            control_path=control_path,
            paths=requested_paths,
            timeout_seconds=max(10.0, float(request.timeout_seconds)),
        )
        resolved_root = resolved_map.get(requested_root, requested_root)
        resolved_cwd = resolved_map.get(requested_cwd, requested_cwd)

        writable_candidates: list[str] = []
        for raw_path in requested_writable:
            writable_candidates.append(raw_path)
            resolved = resolved_map.get(raw_path)
            if resolved:
                writable_candidates.append(resolved)
        writable_candidates.extend([requested_root, resolved_root, requested_cwd, resolved_cwd])

        normalized_writable: list[Path] = []
        seen_paths: set[str] = set()
        for raw_path in writable_candidates:
            normalized = str(self._normalize_remote_posix_path(raw_path))
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            normalized_writable.append(Path(normalized))

        return ShellCommandRequest(
            command=request.command,
            cwd=Path(str(self._normalize_remote_posix_path(resolved_cwd))),
            workspace_root=Path(str(self._normalize_remote_posix_path(resolved_root))),
            writable_paths=normalized_writable,
            timeout_seconds=request.timeout_seconds,
            network_policy=request.network_policy,
            allowed_domains=list(request.allowed_domains),
            environment=dict(request.environment),
            session_id=request.session_id,
            remote=request.remote,
        )

    async def _resolve_remote_runtime_paths(
        self,
        *,
        cache_key: str,
        remote: RemoteShellContext,
        control_path: Path,
        paths: list[str],
        timeout_seconds: float,
    ) -> dict[str, str]:
        normalized_paths: list[str] = []
        seen: set[str] = set()
        for raw_path in paths:
            normalized = str(self._normalize_remote_posix_path(raw_path))
            if normalized in seen:
                continue
            seen.add(normalized)
            normalized_paths.append(normalized)
        if not normalized_paths:
            return {}

        cached = self._remote_resolved_paths.setdefault(cache_key, {})
        unresolved = [path for path in normalized_paths if path not in cached]
        if unresolved:
            resolver_script = (
                "set -euo pipefail; "
                "for raw in \"$@\"; do "
                "resolved=\"$raw\"; "
                "if [ -d \"$raw\" ]; then resolved=$(cd \"$raw\" && /bin/pwd -P); fi; "
                "printf '%s\t%s\\n' \"$raw\" \"$resolved\"; "
                "done"
            )
            resolver_command = shlex.join(
                ["/bin/bash", "--noprofile", "--norc", "-c", resolver_script, "--", *unresolved]
            )
            resolved_result = await self._run_ssh_command(
                remote=remote,
                control_path=control_path,
                command=resolver_command,
                timeout_seconds=max(10.0, float(timeout_seconds)),
                on_event=None,
            )
            if resolved_result.exit_code == 0:
                for line in resolved_result.stdout.splitlines():
                    left, sep, right = line.partition("\t")
                    if not sep:
                        continue
                    try:
                        normalized_left = str(self._normalize_remote_posix_path(left))
                        normalized_right = str(self._normalize_remote_posix_path(right))
                    except SandboxError:
                        continue
                    cached[normalized_left] = normalized_right
            else:
                for path in unresolved:
                    cached[path] = path
        return {path: cached.get(path, path) for path in normalized_paths}

    async def _run_ssh_command(
        self,
        *,
        remote: RemoteShellContext,
        control_path: Path,
        command: str,
        timeout_seconds: float,
        on_event: ShellEventCallback | None,
        stdin_text: str | None = None,
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
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
                env=env,
                start_new_session=os.name != "nt",
            )
            assert process.stdout is not None
            assert process.stderr is not None

            if stdin_text is not None and process.stdin is not None:
                process.stdin.write(stdin_text.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                with contextlib.suppress(Exception):
                    await process.stdin.wait_closed()

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

    def _best_effort_remote_cache_cleanup(
        self,
        remote: RemoteShellContext,
        control_path: Path,
    ) -> None:
        try:
            ssh_command, password_file = self._build_ssh_command(
                remote=remote,
                control_path=control_path,
                command=self._remote_cleanup_script(remote.session_id),
            )
        except Exception:
            return
        try:
            import subprocess

            subprocess.run(
                ssh_command,
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception:
            pass
        finally:
            if password_file is not None:
                password_file.unlink(missing_ok=True)

    def _close_controlmaster_socket(self, cache_key: str, control_path: Path) -> None:
        ssh_target = cache_key.split("::", 1)[1]
        destination = ssh_target
        port: int | None = None
        with contextlib.suppress(Exception):
            user, host, port = parse_ssh_target(ssh_target)
            destination = f"{user}@{host}"
        subprocess_command = [
            "ssh",
            "-o",
            f"ControlPath={str(control_path)}",
        ]
        if port is not None:
            subprocess_command.extend(["-p", str(port)])
        subprocess_command.extend(
            [
                "-O",
                "exit",
                destination,
            ]
        )
        with contextlib.suppress(Exception):
            import subprocess

            subprocess.run(
                subprocess_command,
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
        # Keep ControlPath short to stay within Unix socket length limits when
        # OpenSSH appends temporary suffixes for master connection setup.
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

    @staticmethod
    def _remote_session_cache_dir(session_id: str) -> str:
        return f"${{HOME}}/.opencompany_remote/{str(session_id or '').strip()}"

    @classmethod
    def remote_dependency_guard_script(cls, session_id: str) -> str:
        cache_dir = cls._remote_session_cache_dir(session_id)
        marker_path = f"{cache_dir}/dependencies.ready"
        setup = cls.remote_dependency_setup_script()
        return (
            "set -euo pipefail; "
            f"cache_dir={cache_dir}; "
            f"marker_path={marker_path}; "
            "mkdir -p \"$cache_dir\"; "
            "if [ ! -f \"$marker_path\" ]; then "
            f"{setup}; "
            ": > \"$marker_path\"; "
            "fi"
        )

    def _remote_cleanup_script(self, session_id: str) -> str:
        cache_dir = self._remote_session_cache_dir(session_id)
        return (
            "set -euo pipefail; "
            f"cache_dir={cache_dir}; "
            "mkdir -p \"$cache_dir\"; "
            "rm -f \"$cache_dir\"/exec_*.sh \"$cache_dir\"/*.lock \"$cache_dir\"/*.pid || true"
        )

    @staticmethod
    def remote_dependency_setup_script() -> str:
        return (
            "set -euo pipefail; "
            "PATH=\"$HOME/.local/bin:$HOME/.npm/bin:$PATH\"; "
            "status(){ printf '[opencompany][remote-setup] %s\\n' \"$1\" >&2; }; "
            "have(){ command -v \"$1\" >/dev/null 2>&1; }; "
            "node_major(){ "
            "if ! have node; then echo 0; return 0; fi; "
            "raw=$(node -v 2>/dev/null || true); "
            "raw=${raw#v}; "
            "major=${raw%%.*}; "
            "case \"$major\" in ''|*[!0-9]*) echo 0 ;; *) echo \"$major\" ;; esac; "
            "}; "
            "run_root(){ "
            "if [ \"$(id -u)\" -eq 0 ]; then \"$@\"; return $?; fi; "
            "if have sudo; then sudo -n \"$@\"; return $?; fi; "
            "status 'sudo not available for privileged install'; "
            "return 127; "
            "}; "
            "apt_exec(){ "
            "run_root env DEBIAN_FRONTEND=noninteractive LC_ALL=C LANG=C \"$@\"; "
            "}; "
            "install_pkg(){ "
            "pkg=\"$1\"; "
            "if have apt-get; then "
            "status \"Installing ${pkg} via apt-get\"; "
            "apt_exec apt-get update -y >/dev/null 2>&1 || true; "
            "apt_exec dpkg --configure -a >/dev/null 2>&1 || true; "
            "apt_exec apt-get -f install -y >/dev/null 2>&1 || true; "
            "if apt_exec apt-get install -y \"$pkg\"; then return 0; fi; "
            "status \"apt-get install failed for ${pkg}; attempting dpkg repair and retry\"; "
            "apt_exec dpkg --configure -a >/dev/null 2>&1 || true; "
            "apt_exec apt-get -f install -y >/dev/null 2>&1 || true; "
            "apt_exec apt-get install -y \"$pkg\" && return 0; "
            "return 1; "
            "fi; "
            "if have dnf; then status \"Installing ${pkg} via dnf\"; run_root dnf install -y \"$pkg\" && return 0; fi; "
            "if have yum; then status \"Installing ${pkg} via yum\"; run_root yum install -y \"$pkg\" && return 0; fi; "
            "if have zypper; then status \"Installing ${pkg} via zypper\"; run_root zypper --non-interactive install -y \"$pkg\" && return 0; fi; "
            "if have apk; then status \"Installing ${pkg} via apk\"; run_root apk add --no-cache \"$pkg\" && return 0; fi; "
            "if have pacman; then status \"Installing ${pkg} via pacman\"; run_root pacman -Sy --noconfirm \"$pkg\" && return 0; fi; "
            "status \"No supported package manager found for auto-install (${pkg})\"; "
            "return 126; "
            "}; "
            "ensure_tool(){ "
            "tool=\"$1\"; pkg=\"$2\"; label=\"$3\"; "
            "if have \"$tool\"; then status \"${label} already available\"; return 0; fi; "
            "status \"${label} missing, attempting sudo auto-install (${pkg})\"; "
            "if ! install_pkg \"$pkg\"; then "
            "status \"Auto-install failed for ${label}\"; "
            "return 1; "
            "fi; "
            "if ! have \"$tool\"; then "
            "status \"${label} still missing after install\"; "
            "return 1; "
            "fi; "
            "status \"${label} installed\"; "
            "return 0; "
            "}; "
            "ensure_downloader(){ "
            "if have curl || have wget; then return 0; fi; "
            "status 'curl/wget missing, attempting auto-install'; "
            "if ensure_tool curl curl 'curl'; then return 0; fi; "
            "if ensure_tool wget wget 'wget'; then return 0; fi; "
            "status 'Auto-install failed for curl/wget downloader'; "
            "return 1; "
            "}; "
            "download_file(){ "
            "url=\"$1\"; out=\"$2\"; "
            "if have curl; then curl -fsSL \"$url\" -o \"$out\" && return 0; fi; "
            "if have wget; then wget -q \"$url\" -O \"$out\" && return 0; fi; "
            "return 1; "
            "}; "
            "is_cn_env(){ "
            "locale_hint=\"${LANG:-} ${LC_ALL:-} ${TZ:-}\"; "
            "case \"$locale_hint\" in "
            "*CN*|*cn*|*zh_CN*|*Asia/Shanghai*|*Asia/Chongqing*|*Asia/Harbin*|*Asia/Urumqi*) return 0 ;; "
            "*) return 1 ;; "
            "esac; "
            "}; "
            "install_nodejs_cn_mirror(){ "
            "if ! have apt-get; then return 1; fi; "
            "if ! is_cn_env; then return 1; fi; "
            "if [ ! -r /etc/os-release ]; then status 'Cannot detect distro for CN apt mirror override'; return 1; fi; "
            ". /etc/os-release >/dev/null 2>&1 || true; "
            "distro=\"${ID:-}\"; "
            "codename=\"${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}\"; "
            "if [ -z \"$codename\" ] && have lsb_release; then codename=$(lsb_release -cs 2>/dev/null || true); fi; "
            "if [ -z \"$codename\" ]; then status 'Cannot determine distro codename for CN apt mirror override'; return 1; fi; "
            "tmp_list=$(mktemp 2>/dev/null || mktemp -t opencompany-apt-sources); "
            "if [ -z \"$tmp_list\" ]; then status 'Failed to allocate temp apt source list'; return 1; fi; "
            "case \"$distro\" in "
            "ubuntu) "
            "printf '%s\\n' "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ ${codename} main restricted universe multiverse\" "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ ${codename}-updates main restricted universe multiverse\" "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ ${codename}-backports main restricted universe multiverse\" "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ ${codename}-security main restricted universe multiverse\" "
            "> \"$tmp_list\" ;; "
            "debian) "
            "printf '%s\\n' "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename} main contrib non-free non-free-firmware\" "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename}-updates main contrib non-free non-free-firmware\" "
            "\"deb https://mirrors.tuna.tsinghua.edu.cn/debian-security ${codename}-security main contrib non-free non-free-firmware\" "
            "> \"$tmp_list\" ;; "
            "*) status \"CN apt mirror override not supported for distro (${distro:-unknown})\"; rm -f \"$tmp_list\"; return 1 ;; "
            "esac; "
            "status \"CN environment detected; trying nodejs install via TUNA apt mirror (${distro}:${codename})\"; "
            "apt_opts=\"-o Dir::Etc::sourcelist=$tmp_list -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0\"; "
            "if ! apt_exec sh -lc \"apt-get $apt_opts update -y\"; then "
            "status 'CN mirror apt update failed for nodejs'; "
            "rm -f \"$tmp_list\"; "
            "return 1; "
            "fi; "
            "if ! apt_exec sh -lc \"apt-get $apt_opts install -y nodejs\"; then "
            "status 'CN mirror nodejs install failed'; "
            "rm -f \"$tmp_list\"; "
            "return 1; "
            "fi; "
            "rm -f \"$tmp_list\"; "
            "return 0; "
            "}; "
            "install_nodejs_nodesource_apt(){ "
            "target_major=\"${1:-20}\"; "
            "if ! have apt-get; then return 1; fi; "
            "if [ ! -r /etc/os-release ]; then status 'Cannot detect distro for NodeSource install'; return 1; fi; "
            ". /etc/os-release >/dev/null 2>&1 || true; "
            "distro=\"${ID:-}\"; "
            "case \"$distro\" in ubuntu|debian) ;; *) status \"NodeSource apt install unsupported for distro (${distro:-unknown})\"; return 1 ;; esac; "
            "if ! ensure_downloader; then return 1; fi; "
            "setup_script=$(mktemp 2>/dev/null || mktemp -t opencompany-node-setup); "
            "if [ -z \"$setup_script\" ]; then status 'Failed to allocate temp file for NodeSource setup'; return 1; fi; "
            "status \"Node.js still old; attempting NodeSource node_${target_major}.x apt repo\"; "
            "if ! download_file \"https://deb.nodesource.com/setup_${target_major}.x\" \"$setup_script\"; then "
            "status 'Download failed for NodeSource setup script'; "
            "rm -f \"$setup_script\"; "
            "return 1; "
            "fi; "
            "if ! run_root bash \"$setup_script\"; then "
            "status 'NodeSource setup script failed'; "
            "rm -f \"$setup_script\"; "
            "return 1; "
            "fi; "
            "rm -f \"$setup_script\"; "
            "if ! apt_exec apt-get install -y nodejs; then "
            "status 'NodeSource nodejs install failed'; "
            "return 1; "
            "fi; "
            "return 0; "
            "}; "
            "install_node_tarball_user_space(){ "
            "target_major=\"${1:-20}\"; "
            "if ! ensure_downloader; then return 1; fi; "
            "arch=$(uname -m 2>/dev/null || echo unknown); "
            "case \"$arch\" in "
            "x86_64|amd64) node_arch='x64' ;; "
            "aarch64|arm64) node_arch='arm64' ;; "
            "armv7l) node_arch='armv7l' ;; "
            "*) status \"Unsupported architecture for Node.js tarball (${arch})\"; return 1 ;; "
            "esac; "
            "tmp_dir=$(mktemp -d 2>/dev/null || mktemp -d -t opencompany-node); "
            "if [ -z \"$tmp_dir\" ] || [ ! -d \"$tmp_dir\" ]; then status 'Failed to allocate temp directory for Node.js tarball'; return 1; fi; "
            "sha_file=\"$tmp_dir/SHASUMS256.txt\"; "
            "tar_file=\"$tmp_dir/node.tar.xz\"; "
            "if is_cn_env; then "
            "base_urls='https://mirrors.tuna.tsinghua.edu.cn/nodejs-release https://nodejs.org/dist'; "
            "else "
            "base_urls='https://nodejs.org/dist https://mirrors.tuna.tsinghua.edu.cn/nodejs-release'; "
            "fi; "
            "for base_url in $base_urls; do "
            "status \"Trying Node.js tarball fallback from ${base_url} (v${target_major}.x ${node_arch})\"; "
            "if ! download_file \"$base_url/latest-v${target_major}.x/SHASUMS256.txt\" \"$sha_file\"; then "
            "status 'Node.js tarball manifest download failed'; "
            "continue; "
            "fi; "
            "tarball=$(grep \"linux-${node_arch}\\\\.tar\\\\.xz$\" \"$sha_file\" 2>/dev/null | head -n 1 | awk '{print $2}' || true); "
            "if [ -z \"$tarball\" ]; then "
            "status \"Node.js manifest missing linux-${node_arch}.tar.xz\"; "
            "continue; "
            "fi; "
            "if ! download_file \"$base_url/latest-v${target_major}.x/${tarball}\" \"$tar_file\"; then "
            "status 'Node.js tarball download failed'; "
            "continue; "
            "fi; "
            "if ! tar -xJf \"$tar_file\" -C \"$tmp_dir\"; then "
            "status 'Node.js tarball extract failed'; "
            "continue; "
            "fi; "
            "extracted_dir=\"$tmp_dir/${tarball%.tar.xz}\"; "
            "if [ ! -d \"$extracted_dir\" ]; then "
            "status 'Node.js tarball extraction output missing'; "
            "continue; "
            "fi; "
            "install_dir=\"$HOME/.local/node-v${target_major}\"; "
            "install_tmp=\"$install_dir.tmp\"; "
            "mkdir -p \"$HOME/.local/bin\"; "
            "rm -rf \"$install_tmp\" >/dev/null 2>&1 || true; "
            "mkdir -p \"$install_tmp\"; "
            "if ! cp -R \"$extracted_dir/.\" \"$install_tmp/\"; then "
            "status 'Failed to copy Node.js tarball into user space'; "
            "rm -rf \"$install_tmp\" >/dev/null 2>&1 || true; "
            "continue; "
            "fi; "
            "rm -rf \"$install_dir\" >/dev/null 2>&1 || true; "
            "mv \"$install_tmp\" \"$install_dir\"; "
            "ln -sf \"$install_dir/bin/node\" \"$HOME/.local/bin/node\"; "
            "ln -sf \"$install_dir/bin/npm\" \"$HOME/.local/bin/npm\"; "
            "ln -sf \"$install_dir/bin/npx\" \"$HOME/.local/bin/npx\"; "
            "if [ -x \"$install_dir/bin/corepack\" ]; then "
            "ln -sf \"$install_dir/bin/corepack\" \"$HOME/.local/bin/corepack\"; "
            "fi; "
            "major=$(node_major); "
            "if [ \"$major\" -ge 18 ] 2>/dev/null; then "
            "status \"Node.js >= 18 ready via user-space tarball (${major})\"; "
            "rm -rf \"$tmp_dir\" >/dev/null 2>&1 || true; "
            "return 0; "
            "fi; "
            "status \"Node.js still too old after tarball install (${major})\"; "
            "done; "
            "rm -rf \"$tmp_dir\" >/dev/null 2>&1 || true; "
            "return 1; "
            "}; "
            "ensure_node18(){ "
            "major=$(node_major); "
            "if [ \"$major\" -ge 18 ] 2>/dev/null; then "
            "status \"Node.js >= 18 already available (${major})\"; "
            "return 0; "
            "fi; "
            "status \"Node.js >= 18 required, attempting system package install (nodejs)\"; "
            "if ! install_nodejs_cn_mirror; then "
            "if ! install_pkg nodejs; then "
            "status 'Auto-install failed for Node.js package'; "
            "return 1; "
            "fi; "
            "fi; "
            "major=$(node_major); "
            "if [ \"$major\" -lt 18 ] 2>/dev/null; then "
            "status \"Node.js remains too old after system package install (${major}); trying NodeSource node_20.x\"; "
            "if ! install_nodejs_nodesource_apt 20; then "
            "status 'NodeSource node_20.x install unavailable or failed'; "
            "fi; "
            "major=$(node_major); "
            "if [ \"$major\" -lt 18 ] 2>/dev/null; then "
            "status \"Node.js remains too old after NodeSource install (${major}); trying user-space tarball fallback\"; "
            "if ! install_node_tarball_user_space 20; then "
            "status 'Node.js user-space tarball fallback failed'; "
            "fi; "
            "fi; "
            "major=$(node_major); "
            "if [ \"$major\" -lt 18 ] 2>/dev/null; then "
            "status \"Node.js remains too old after fallback chain (${major})\"; "
            "return 1; "
            "fi; "
            "fi; "
            "status \"Node.js >= 18 ready (${major})\"; "
            "return 0; "
            "}; "
            "status 'Checking remote sandbox dependencies (rg, npm, srt, bwrap, socat)'; "
            "ensure_tool rg ripgrep 'ripgrep (rg)' || { "
            "echo 'Missing dependency: rg. Auto-install failed. Install ripgrep on remote host.' >&2; "
            "exit 18; "
            "}; "
            "ensure_tool bwrap bubblewrap 'bubblewrap (bwrap)' || { "
            "echo 'Missing dependency: bubblewrap (bwrap). Auto-install failed. Install bubblewrap on remote host.' >&2; "
            "exit 22; "
            "}; "
            "ensure_tool socat socat 'socat' || { "
            "echo 'Missing dependency: socat. Auto-install failed. Install socat on remote host.' >&2; "
            "exit 23; "
            "}; "
            "ensure_node18 || { "
            "echo 'Missing dependency: Node.js >= 18. Auto-install failed or version too old.' >&2; "
            "exit 20; "
            "}; "
            "if ! have srt; then "
            "if ! have npm; then "
            "ensure_tool npm npm 'npm' || { "
            "echo 'Missing dependency: npm. Auto-install failed. Install npm on remote host.' >&2; "
            "exit 19; "
            "}; "
            "fi; "
            "status 'Installing srt in user space with npm'; "
            "npm config set prefix \"$HOME/.local\" >/dev/null 2>&1 || true; "
            "if ! npm install -g @anthropic-ai/sandbox-runtime; then "
            "echo 'Missing dependency: srt. npm install failed in user space.' >&2; "
            "exit 17; "
            "fi; "
            "fi; "
            "if ! have srt; then "
            "echo 'Missing dependency: srt. Install with npm in user space.' >&2; "
            "exit 17; "
            "fi; "
            "status 'Checking bubblewrap runtime capability'; "
            "if ! bwrap --unshare-user --uid 0 --gid 0 --ro-bind / / --proc /proc --dev /dev /bin/true >/dev/null 2>&1; then "
            "userns='unknown'; "
            "if [ -r /proc/sys/kernel/unprivileged_userns_clone ]; then "
            "userns=$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo unknown); "
            "fi; "
            "if [ \"$userns\" = '0' ]; then "
            "echo 'Sandbox runtime unsupported: bubblewrap cannot create user namespace (kernel.unprivileged_userns_clone=0). Enable user namespaces on remote host.' >&2; "
            "else "
            "echo 'Sandbox runtime unsupported: bubblewrap cannot create new namespace (Operation not permitted). Ensure host/container allows unprivileged user namespaces.' >&2; "
            "fi; "
            "exit 24; "
            "fi; "
            "if ! srt --help >/dev/null 2>&1; then "
            "echo 'Dependency runtime error: srt failed to start. Ensure Node.js >= 18 on remote host.' >&2; "
            "exit 21; "
            "fi; "
            "status 'Remote sandbox dependencies ready'"
        )

    @staticmethod
    def _dependency_check_script() -> str:
        # Backward-compatible alias for existing tests/call-sites.
        return AnthropicSandboxBackend.remote_dependency_setup_script()

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
