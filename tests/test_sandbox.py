from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from opencompany.config import SandboxConfig
from opencompany.models import RemoteSessionConfig, RemoteShellContext, ShellCommandRequest, ShellCommandResult
from opencompany.sandbox.anthropic import AnthropicSandboxBackend
from opencompany.sandbox.base import SandboxError
from opencompany.sandbox.none import NoSandboxBackend


class AnthropicSandboxBackendTests(unittest.TestCase):
    def test_build_settings_includes_resolved_ripgrep_command(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            fake_rg = root / "bin" / "rg"
            app_dir.mkdir()
            workspace.mkdir()
            fake_rg.parent.mkdir()
            fake_rg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
            )

            with unittest.mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value=str(fake_rg),
            ):
                settings = backend.build_settings(request)

            self.assertEqual(settings["ripgrep"]["command"], str(fake_rg.resolve()))

    def test_build_settings_requires_ripgrep(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            fake_python = root / "bin" / "python"
            app_dir.mkdir()
            workspace.mkdir()
            fake_python.parent.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
            )

            with unittest.mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value=None,
            ), unittest.mock.patch(
                "opencompany.sandbox.anthropic.sys.executable",
                str(fake_python),
            ):
                with self.assertRaises(SandboxError):
                    backend.build_settings(request)

    def test_build_settings_skips_local_proxy_startup_when_no_domains_allowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            fake_rg = root / "bin" / "rg"
            app_dir.mkdir()
            workspace.mkdir()
            fake_rg.parent.mkdir()
            fake_rg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
                network_policy="deny_all",
            )

            with unittest.mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value=str(fake_rg),
            ):
                settings = backend.build_settings(request)

            network = settings.get("network", {})
            self.assertEqual(network.get("allowedDomains"), [])
            self.assertEqual(network.get("httpProxyPort"), 65535)
            self.assertEqual(network.get("socksProxyPort"), 65534)

    def test_build_settings_allow_all_policy_sets_wildcard_domain(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            fake_rg = root / "bin" / "rg"
            app_dir.mkdir()
            workspace.mkdir()
            fake_rg.parent.mkdir()
            fake_rg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
                network_policy="allow_all",
            )

            with unittest.mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value=str(fake_rg),
            ):
                settings = backend.build_settings(request)

            network = settings.get("network", {})
            self.assertEqual(network.get("allowedDomains"), ["*"])
            self.assertNotIn("httpProxyPort", network)
            self.assertNotIn("socksProxyPort", network)

    def test_build_settings_allowlist_policy_uses_explicit_domains(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            fake_rg = root / "bin" / "rg"
            app_dir.mkdir()
            workspace.mkdir()
            fake_rg.parent.mkdir()
            fake_rg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
                network_policy="allowlist",
                allowed_domains=["example.com", "openai.com"],
            )

            with unittest.mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value=str(fake_rg),
            ):
                settings = backend.build_settings(request)

            network = settings.get("network", {})
            self.assertEqual(network.get("allowedDomains"), ["example.com", "openai.com"])
            self.assertNotIn("httpProxyPort", network)
            self.assertNotIn("socksProxyPort", network)

    def test_build_settings_allowlist_requires_non_empty_domains(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
                network_policy="allowlist",
                allowed_domains=[],
            )

            with self.assertRaisesRegex(SandboxError, "requires non-empty allowed_domains"):
                backend.build_settings(request)

    def test_build_settings_remote_preserves_posix_allow_write_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="pwd",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )

            settings = backend.build_settings(request)

            allow_write = settings["filesystem"]["allowWrite"]
            self.assertIn("/home/demo/workspace", allow_write)
            self.assertNotIn("/System/Volumes/Data/home/demo/workspace", allow_write)
            self.assertEqual(settings["ripgrep"]["command"], "rg")


class NoSandboxBackendTests(unittest.TestCase):
    def test_build_settings_returns_empty_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
            )

            self.assertEqual(backend.build_settings(request), {})
            self.assertFalse(backend.should_block_outside_workspace_write())

    def test_build_terminal_command_returns_plain_bash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            request = ShellCommandRequest(
                command="pwd",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
            )

            command = backend.build_terminal_command(
                request,
                settings_path=workspace / "settings.json",
            )

            self.assertEqual(command, "/bin/bash --noprofile --norc -i")


class NoSandboxBackendRunCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_executes_local_bash_without_srt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            request = ShellCommandRequest(
                command="echo hello-none",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=5,
            )

            result = await backend.run_command(request)

            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello-none", result.stdout)
            self.assertFalse(result.timed_out)

    async def test_run_command_timeout_returns_diagnostics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            request = ShellCommandRequest(
                command="sleep 5",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=0.01,
            )

            result = await backend.run_command(request)

            self.assertTrue(result.timed_out)
            self.assertTrue(result.killed)
            self.assertIn("force-terminated", result.stderr)


class NoSandboxBackendRemoteTests(unittest.IsolatedAsyncioTestCase):
    def test_build_ssh_command_key_auth_uses_explicit_port_and_destination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:33885",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            command, password_file = backend._build_ssh_command(
                remote=remote,
                control_path=root / "ssh.sock",
                command="echo ok",
            )

            self.assertIsNone(password_file)
            self.assertIn("-p", command)
            port_index = command.index("-p")
            self.assertEqual(command[port_index + 1], "33885")
            self.assertIn("demo@example.com", command)
            self.assertNotIn("demo@example.com:33885", command)

    def test_build_ssh_command_password_auth_uses_explicit_port_and_destination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:33885",
                    remote_dir="/home/demo/workspace",
                    auth_mode="password",
                    identity_file="",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
                password="secret-pass",
            )
            with mock.patch(
                "opencompany.sandbox.none.shutil.which",
                return_value="/usr/bin/sshpass",
            ):
                command, password_file = backend._build_ssh_command(
                    remote=remote,
                    control_path=root / "ssh.sock",
                    command="echo ok",
                )

            try:
                self.assertIsNotNone(password_file)
                self.assertIn("-p", command)
                port_index = command.index("-p")
                self.assertEqual(command[port_index + 1], "33885")
                self.assertIn("demo@example.com", command)
                self.assertNotIn("demo@example.com:33885", command)
            finally:
                if password_file is not None:
                    password_file.unlink(missing_ok=True)

    async def test_run_command_remote_exec_does_not_use_srt_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )
            captured: dict[str, str] = {}

            async def fake_run_ssh_command(  # type: ignore[no-untyped-def]
                *,
                remote,
                control_path,
                command,
                timeout_seconds,
                on_event,
            ):
                del remote, control_path, timeout_seconds, on_event
                captured["command"] = command
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    command=command,
                )

            with mock.patch.object(
                backend,
                "_run_ssh_command",
                side_effect=fake_run_ssh_command,
            ):
                result = await backend.run_command(request)

            self.assertEqual(result.exit_code, 0)
            self.assertIn("exec /bin/bash --noprofile --norc -c", captured["command"])
            self.assertNotIn("srt --settings", captured["command"])

    def test_cleanup_session_closes_controlmaster(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = NoSandboxBackend(SandboxConfig(backend="none"), app_dir)
            cache_key = "session-1::demo@example.com:22"
            control_path = root / "cm.sock"
            backend._remote_control_paths[cache_key] = control_path
            backend._remote_contexts[cache_key] = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            with mock.patch.object(backend, "_close_controlmaster_socket") as close_socket:
                backend.cleanup_session("session-1")

            close_socket.assert_called_once_with(cache_key, control_path)
            self.assertNotIn(cache_key, backend._remote_control_paths)
            self.assertNotIn(cache_key, backend._remote_contexts)


class _HangingStream:
    async def readline(self) -> bytes:
        await asyncio.Future()


class _ClosedStream:
    async def readline(self) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.stdout = _HangingStream()
        self.stderr = _HangingStream()
        self._done = asyncio.Event()
        if returncode is not None:
            self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def finish(self, returncode: int) -> None:
        self.returncode = returncode
        self._done.set()

    def kill(self) -> None:
        self.finish(-9)


class _CompletedProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = 0
        self.stdout = _ClosedStream()
        self.stderr = _ClosedStream()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


class AnthropicSandboxBackendRunCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_times_out_kills_process_and_returns_diagnostics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="sleep 999",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=0.01,
            )
            process = _FakeProcess()

            async def fake_create_subprocess_exec(*args, **kwargs):
                del args, kwargs
                return process

            with mock.patch.object(backend, "resolve_cli_path", return_value="/fake/srt"), mock.patch.object(
                backend,
                "build_settings",
                return_value={},
            ), mock.patch(
                "opencompany.sandbox.anthropic.asyncio.create_subprocess_exec",
                new=fake_create_subprocess_exec,
            ), mock.patch(
                "opencompany.sandbox.anthropic.os.killpg",
                side_effect=lambda pid, sig: process.finish(-9),
            ), mock.patch.object(
                backend,
                "STREAM_DRAIN_TIMEOUT_SECONDS",
                0.01,
            ), mock.patch.object(
                backend,
                "PROCESS_KILL_TIMEOUT_SECONDS",
                0.01,
            ):
                result = await backend.run_command(request)

            self.assertTrue(result.timed_out)
            self.assertTrue(result.killed)
            self.assertTrue(result.reader_tasks_cancelled)
            self.assertEqual(result.exit_code, -9)
            self.assertEqual(result.termination_reason, "process_group_killed")
            self.assertEqual(result.timeout_seconds, 0.01)
            self.assertIn("force-terminated", result.stderr)

    async def test_run_command_returns_after_process_exit_even_if_streams_never_close(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="true",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=1,
            )
            process = _FakeProcess(returncode=0)

            async def fake_create_subprocess_exec(*args, **kwargs):
                del args, kwargs
                return process

            with mock.patch.object(backend, "resolve_cli_path", return_value="/fake/srt"), mock.patch.object(
                backend,
                "build_settings",
                return_value={},
            ), mock.patch(
                "opencompany.sandbox.anthropic.asyncio.create_subprocess_exec",
                new=fake_create_subprocess_exec,
            ), mock.patch.object(
                backend,
                "STREAM_DRAIN_TIMEOUT_SECONDS",
                0.01,
            ):
                result = await backend.run_command(request)

            self.assertFalse(result.timed_out)
            self.assertFalse(result.killed)
            self.assertTrue(result.reader_tasks_cancelled)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("returning partial output", result.stderr)

    async def test_run_command_uses_clean_non_login_bash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="echo hello",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=1,
                environment={"BASH_ENV": "/tmp/bash_env", "ENV": "/tmp/env", "FOO": "bar"},
            )
            process = _CompletedProcess()
            captured: dict[str, object] = {}

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return process

            with mock.patch.object(backend, "resolve_cli_path", return_value="/fake/srt"), mock.patch.object(
                backend,
                "build_settings",
                return_value={},
            ), mock.patch(
                "opencompany.sandbox.anthropic.asyncio.create_subprocess_exec",
                new=fake_create_subprocess_exec,
            ):
                result = await backend.run_command(request)

            self.assertEqual(result.exit_code, 0)
            args = captured["args"]
            assert isinstance(args, tuple)
            self.assertEqual(
                args[0:4],
                (
                    "/fake/srt",
                    "--settings",
                    args[2],
                    AnthropicSandboxBackend.build_sandbox_command("echo hello"),
                ),
            )
            self.assertEqual(len(args), 4)
            kwargs = captured["kwargs"]
            assert isinstance(kwargs, dict)
            env = kwargs["env"]
            assert isinstance(env, dict)
            self.assertEqual(env["FOO"], "bar")
            self.assertNotIn("BASH_ENV", env)
            self.assertNotIn("ENV", env)

    async def test_run_command_cancellation_terminates_process(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            workspace = root / "workspace"
            app_dir.mkdir()
            workspace.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            request = ShellCommandRequest(
                command="sleep 999",
                cwd=workspace,
                workspace_root=workspace,
                writable_paths=[workspace],
                timeout_seconds=30,
            )
            process = _FakeProcess()
            termination_calls: list[bool] = []

            async def fake_create_subprocess_exec(*args, **kwargs):
                del args, kwargs
                return process

            async def fake_terminate(proc):  # type: ignore[no-untyped-def]
                termination_calls.append(True)
                proc.finish(-9)
                return "process_killed"

            with mock.patch.object(backend, "resolve_cli_path", return_value="/fake/srt"), mock.patch.object(
                backend,
                "build_settings",
                return_value={},
            ), mock.patch(
                "opencompany.sandbox.anthropic.asyncio.create_subprocess_exec",
                new=fake_create_subprocess_exec,
            ), mock.patch.object(
                backend,
                "_terminate_process",
                side_effect=fake_terminate,
            ), mock.patch.object(
                backend,
                "STREAM_DRAIN_TIMEOUT_SECONDS",
                0.01,
            ):
                task = asyncio.create_task(backend.run_command(request))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertEqual(len(termination_calls), 1)


class AnthropicSandboxBackendRemoteTests(unittest.IsolatedAsyncioTestCase):
    def test_dependency_setup_script_includes_sudo_auto_install_and_status(self) -> None:
        script = AnthropicSandboxBackend.remote_dependency_setup_script()
        self.assertIn("sudo -n", script)
        self.assertIn("ensure_tool rg ripgrep", script)
        self.assertIn("ensure_tool bwrap bubblewrap", script)
        self.assertIn("ensure_tool socat socat", script)
        self.assertIn("Checking bubblewrap runtime capability", script)
        self.assertIn("unprivileged_userns_clone", script)
        self.assertIn("ensure_tool npm npm", script)
        self.assertIn("ensure_node18", script)
        self.assertIn("Node.js >= 18", script)
        self.assertIn("install_nodejs_cn_mirror", script)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/ubuntu", script)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/debian", script)
        self.assertIn("attempting system package install (nodejs)", script)
        self.assertIn("install_nodejs_nodesource_apt", script)
        self.assertIn("NodeSource node_20.x", script)
        self.assertIn("install_node_tarball_user_space", script)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/nodejs-release", script)
        self.assertNotIn("install_node_with_n", script)
        self.assertIn("DEBIAN_FRONTEND=noninteractive", script)
        self.assertIn("dpkg --configure -a", script)
        self.assertIn("apt-get -f install -y", script)
        self.assertIn("srt --help", script)
        self.assertIn("[opencompany][remote-setup]", script)

    def test_remote_control_path_for_key_uses_short_socket_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)

            control_path = backend._remote_control_path_for_key("session-1::demo@example.com:33885")

            if os.name != "nt":
                self.assertTrue(str(control_path).startswith("/tmp/opencompany-ssh/"))
                self.assertLess(len(str(control_path)), 90)

    def test_build_ssh_command_key_auth_uses_explicit_port_and_destination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:33885",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )

            command, password_file = backend._build_ssh_command(
                remote=remote,
                control_path=root / "ssh.sock",
                command="echo ok",
            )

            self.assertIsNone(password_file)
            self.assertIn("-p", command)
            port_index = command.index("-p")
            self.assertEqual(command[port_index + 1], "33885")
            self.assertIn("demo@example.com", command)
            self.assertNotIn("demo@example.com:33885", command)

    def test_build_ssh_command_password_auth_uses_explicit_port_and_destination(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:33885",
                    remote_dir="/home/demo/workspace",
                    auth_mode="password",
                    identity_file="",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
                password="secret-pass",
            )
            with mock.patch(
                "opencompany.sandbox.anthropic.shutil.which",
                return_value="/usr/bin/sshpass",
            ):
                command, password_file = backend._build_ssh_command(
                    remote=remote,
                    control_path=root / "ssh.sock",
                    command="echo ok",
                )

            try:
                self.assertIsNotNone(password_file)
                self.assertIn("-p", command)
                port_index = command.index("-p")
                self.assertEqual(command[port_index + 1], "33885")
                self.assertIn("demo@example.com", command)
                self.assertNotIn("demo@example.com:33885", command)
            finally:
                if password_file is not None:
                    password_file.unlink(missing_ok=True)

    async def test_remote_dependency_check_streams_setup_status(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )
            streamed: list[tuple[str, str]] = []
            dependency_timeouts: list[float] = []

            async def fake_run_ssh_command(  # type: ignore[no-untyped-def]
                *,
                remote,
                control_path,
                command,
                timeout_seconds,
                on_event,
                stdin_text=None,
            ):
                del remote, control_path, stdin_text
                if "[opencompany][remote-setup]" in command:
                    dependency_timeouts.append(float(timeout_seconds))
                    self.assertIsNotNone(on_event)
                    assert on_event is not None
                    maybe = on_event("stderr", "[opencompany][remote-setup] Checking dependencies\n")
                    if asyncio.iscoroutine(maybe):
                        await maybe
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok\n",
                    stderr="",
                    command=command,
                )

            async def on_event(channel: str, text: str) -> None:
                streamed.append((channel, text))

            with mock.patch.object(
                backend,
                "build_settings",
                return_value={"network": {}, "filesystem": {}, "ripgrep": {"command": "/usr/bin/rg"}},
            ), mock.patch.object(
                backend,
                "_run_ssh_command",
                side_effect=fake_run_ssh_command,
            ):
                result = await backend.run_command(request, on_event=on_event)

            self.assertEqual(result.exit_code, 0)
            self.assertTrue(any("[opencompany][remote-setup]" in text for _channel, text in streamed))
            self.assertTrue(dependency_timeouts)
            self.assertGreaterEqual(
                dependency_timeouts[0],
                AnthropicSandboxBackend.REMOTE_DEPENDENCY_TIMEOUT_SECONDS,
            )

    async def test_remote_run_reuses_dependency_and_settings_between_commands(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )
            commands: list[str] = []

            async def fake_run_ssh_command(  # type: ignore[no-untyped-def]
                *,
                remote,
                control_path,
                command,
                timeout_seconds,
                on_event,
                stdin_text=None,
            ):
                del remote, control_path, timeout_seconds, on_event, stdin_text
                commands.append(command)
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok\n",
                    stderr="",
                    command=command,
                )

            with mock.patch.object(
                backend,
                "build_settings",
                return_value={"network": {}, "filesystem": {}, "ripgrep": {"command": "/usr/bin/rg"}},
            ), mock.patch.object(
                backend,
                "_run_ssh_command",
                side_effect=fake_run_ssh_command,
            ):
                first = await backend.run_command(request)
                second = await backend.run_command(request)

            self.assertEqual(first.exit_code, 0)
            self.assertEqual(second.exit_code, 0)
            dependency_calls = [
                command
                for command in commands
                if "npm install -g @anthropic-ai/sandbox-runtime" in command
            ]
            settings_calls = [
                command
                for command in commands
                if 'cat > "$remote_settings_path".tmp' in command
            ]
            exec_calls = [
                command
                for command in commands
                if "exec srt --settings" in command
            ]
            self.assertEqual(len(dependency_calls), 1)
            self.assertEqual(len(settings_calls), 1)
            self.assertEqual(len(exec_calls), 2)
            self.assertTrue(
                all("exec srt --settings ${HOME}/.opencompany_remote/session-1/settings.json" in cmd for cmd in exec_calls),
                msg="Remote exec command should pass an expanded ${HOME}-based settings path, not a single-quoted literal.",
            )

    async def test_prepare_remote_runtime_request_includes_resolved_alias_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )
            expected_map = {
                "/home/demo/workspace": "/srv/project/workspace",
            }

            with mock.patch.object(
                backend,
                "_resolve_remote_runtime_paths",
                return_value=expected_map,
            ):
                runtime_request = await backend._prepare_remote_runtime_request(
                    cache_key="session-1::demo@example.com:22",
                    request=request,
                    remote=remote,
                    control_path=Path("/tmp/opencompany-ssh/cm-demo"),
                )

            self.assertEqual(str(runtime_request.workspace_root), "/srv/project/workspace")
            self.assertEqual(str(runtime_request.cwd), "/srv/project/workspace")
            self.assertIn(
                "/home/demo/workspace",
                [str(path) for path in runtime_request.writable_paths],
            )
            self.assertIn(
                "/srv/project/workspace",
                [str(path) for path in runtime_request.writable_paths],
            )

    async def test_run_remote_command_uses_prepared_runtime_request_for_settings_and_cd(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/home/demo/workspace"),
                workspace_root=Path("/home/demo/workspace"),
                writable_paths=[Path("/home/demo/workspace")],
                timeout_seconds=30,
                remote=remote,
            )
            runtime_request = ShellCommandRequest(
                command="echo hello",
                cwd=Path("/srv/project/workspace"),
                workspace_root=Path("/srv/project/workspace"),
                writable_paths=[
                    Path("/home/demo/workspace"),
                    Path("/srv/project/workspace"),
                ],
                timeout_seconds=30,
                remote=remote,
            )
            commands: list[str] = []

            async def fake_run_ssh_command(  # type: ignore[no-untyped-def]
                *,
                remote,
                control_path,
                command,
                timeout_seconds,
                on_event,
                stdin_text=None,
            ):
                del remote, control_path, timeout_seconds, on_event, stdin_text
                commands.append(command)
                return ShellCommandResult(
                    exit_code=0,
                    stdout="ok\n",
                    stderr="",
                    command=command,
                )

            with mock.patch.object(
                backend,
                "_prepare_remote_runtime_request",
                return_value=runtime_request,
            ), mock.patch.object(
                backend,
                "build_settings",
                return_value={"network": {}, "filesystem": {}, "ripgrep": {"command": "rg"}},
            ) as build_settings_mock, mock.patch.object(
                backend,
                "_run_ssh_command",
                side_effect=fake_run_ssh_command,
            ):
                result = await backend.run_command(request)

            self.assertEqual(result.exit_code, 0)
            build_settings_mock.assert_called_once_with(runtime_request)
            self.assertTrue(
                any("cd /srv/project/workspace;" in command for command in commands),
                msg="Remote exec command should cd into prepared runtime cwd.",
            )

    async def test_run_ssh_command_deletes_password_temp_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="password",
                    identity_file="",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
                password="secret",
            )
            password_file = root / "password.tmp"
            password_file.write_text("secret", encoding="utf-8")

            async def fake_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return _CompletedProcess()

            with mock.patch.object(
                backend,
                "_build_ssh_command",
                return_value=(["ssh", "demo@example.com", "true"], password_file),
            ), mock.patch(
                "opencompany.sandbox.anthropic.asyncio.create_subprocess_exec",
                new=fake_create_subprocess_exec,
            ):
                result = await backend._run_ssh_command(
                    remote=remote,
                    control_path=root / "ssh.sock",
                    command="true",
                    timeout_seconds=5,
                    on_event=None,
                )

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(password_file.exists())

    def test_cleanup_session_clears_remote_runtime_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "app"
            app_dir.mkdir()
            backend = AnthropicSandboxBackend(SandboxConfig(), app_dir)
            cache_key = "session-1::demo@example.com:22"
            control_path = app_dir / "socket.sock"
            remote = RemoteShellContext(
                session_id="session-1",
                config=RemoteSessionConfig(
                    kind="remote_ssh",
                    ssh_target="demo@example.com:22",
                    remote_dir="/home/demo/workspace",
                    auth_mode="key",
                    identity_file="~/.ssh/id_ed25519",
                    known_hosts_policy="accept_new",
                    remote_os="linux",
                ),
            )
            backend._remote_control_paths[cache_key] = control_path
            backend._remote_contexts[cache_key] = remote
            backend._remote_dependency_checked.add(cache_key)
            backend._remote_settings_hash[cache_key] = "abc"
            backend._remote_gc_checked.add(cache_key)
            backend._remote_resolved_paths[cache_key] = {"/home/demo/workspace": "/srv/demo"}

            with mock.patch.object(
                backend,
                "_best_effort_remote_cache_cleanup",
            ) as cleanup_mock, mock.patch.object(
                backend,
                "_close_controlmaster_socket",
            ) as close_mock:
                backend.cleanup_session("session-1")

            cleanup_mock.assert_called_once()
            close_mock.assert_called_once()
            self.assertNotIn(cache_key, backend._remote_control_paths)
            self.assertNotIn(cache_key, backend._remote_contexts)
            self.assertNotIn(cache_key, backend._remote_dependency_checked)
            self.assertNotIn(cache_key, backend._remote_settings_hash)
            self.assertNotIn(cache_key, backend._remote_gc_checked)
            self.assertNotIn(cache_key, backend._remote_resolved_paths)


class AnthropicSandboxBackendCommandQuotingTests(unittest.TestCase):
    def _run_wrapped_command(self, cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        return subprocess.run(
            ["/bin/sh", "-c", AnthropicSandboxBackend.build_sandbox_command(command)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )

    @staticmethod
    def _simulate_outer_quote_layer(command: str) -> str:
        # Match shell-quote's behavior used by `srt` for a double-quoted `-c` payload.
        escaped = (
            command.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
            .replace("!", "\\!")
        )
        return f'/bin/bash -c "{escaped}"'

    def test_wrapped_command_preserves_single_directory_argument(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = self._run_wrapped_command(workspace, "mkdir test")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / "test").is_dir())

    def test_wrapped_command_preserves_p_flag_and_nested_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = self._run_wrapped_command(workspace, "mkdir -p test/java")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / "test" / "java").is_dir())

    def test_wrapped_command_preserves_shell_control_operators(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = self._run_wrapped_command(workspace, "pwd && ls -la")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(workspace), result.stdout)
            self.assertIn("total", result.stdout)

    def test_wrapped_command_preserves_redirection_payload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = self._run_wrapped_command(workspace, 'echo "test" > dummy.txt')

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((workspace / "dummy.txt").read_text(encoding="utf-8"), "test\n")

    def test_wrapped_command_survives_outer_quote_layer_that_escapes_bang(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            user_command = (
                "cat > bang.c << 'EOF'\n"
                "if (!x) return 0;\n"
                "if (a != b) return 1;\n"
                "EOF"
            )
            wrapped = AnthropicSandboxBackend.build_sandbox_command(user_command)
            simulated_outer = self._simulate_outer_quote_layer(wrapped)

            env = os.environ.copy()
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            result = subprocess.run(
                ["/bin/sh", "-c", simulated_outer],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (workspace / "bang.c").read_text(encoding="utf-8"),
                "if (!x) return 0;\nif (a != b) return 1;\n",
            )

    @unittest.skipIf(shutil.which("python3") is None, "python3 is not available")
    def test_wrapped_command_preserves_python_version_flag(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = self._run_wrapped_command(workspace, "python3 --version")

            self.assertEqual(result.returncode, 0, result.stderr)
            combined_output = result.stdout + result.stderr
            self.assertIn("Python 3", combined_output)
            self.assertNotIn(">>>", combined_output)
