from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from opencompany.config import SandboxConfig
from opencompany.models import ShellCommandRequest, ShellCommandResult
from opencompany.orchestrator import Orchestrator
from opencompany.sandbox.anthropic import AnthropicSandboxBackend
from opencompany.sandbox.base import SandboxBackend
from opencompany.sandbox.none import NoSandboxBackend
from opencompany.sandbox.registry import (
    register_sandbox_backend,
    resolve_sandbox_backend_cls,
)


class _FakeSandboxBackend(SandboxBackend):
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def resolve_cli_path(self) -> str:
        return "/fake/sandbox"

    def build_settings(self, request: ShellCommandRequest) -> dict:
        del request
        return {}

    async def run_command(
        self,
        request: ShellCommandRequest,
        on_event=None,  # type: ignore[no-untyped-def]
    ) -> ShellCommandResult:
        del request, on_event
        return ShellCommandResult(exit_code=0, stdout="", stderr="", command=":")


class SandboxRegistryTests(unittest.TestCase):
    def test_resolve_default_anthropic_backend(self) -> None:
        resolved = resolve_sandbox_backend_cls(SandboxConfig(backend="anthropic"))
        self.assertIs(resolved, AnthropicSandboxBackend)

    def test_resolve_none_backend(self) -> None:
        resolved = resolve_sandbox_backend_cls(SandboxConfig(backend="none"))
        self.assertIs(resolved, NoSandboxBackend)

    def test_register_custom_backend_and_resolve(self) -> None:
        backend_name = "fake-test"
        register_sandbox_backend(backend_name, _FakeSandboxBackend)
        resolved = resolve_sandbox_backend_cls(SandboxConfig(backend=backend_name))
        self.assertIs(resolved, _FakeSandboxBackend)

    def test_orchestrator_fails_fast_for_unknown_backend(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "prompts").mkdir()
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "unknown-backend"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unsupported sandbox backend"):
                Orchestrator(project_dir, app_dir=project_dir)
