from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opencompany.config import OpenCompanyConfig
from opencompany.utils import load_project_env


class ConfigTests(unittest.TestCase):
    def test_locale_falls_back_to_detected_system_locale(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            config = OpenCompanyConfig.load(project_dir)
            with patch("opencompany.config.detect_system_locale", return_value="zh"):
                self.assertEqual(config.resolve_locale(None), "zh")

    def test_locale_uses_explicit_request_when_supported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = OpenCompanyConfig.load(Path(temp_dir))
            self.assertEqual(config.resolve_locale("en"), "en")
            self.assertEqual(config.runtime.tools.shell_inline_wait_seconds, 5.0)

    def test_project_env_loader_sets_missing_values(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / ".env").write_text(
                "OPENROUTER_API_KEY=test-key\nOTHER_VALUE=demo\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                loaded = load_project_env(project_dir)
                self.assertEqual(loaded["OPENROUTER_API_KEY"], "test-key")

    def test_runtime_tool_timeouts_are_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.tool_timeouts]
default_seconds = 12
shell_seconds = 0

[runtime.tool_timeouts.actions]
wait_time = 5
spawn_agent = 90

[sandbox]
timeout_seconds = 77
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)

            self.assertEqual(config.runtime.tool_timeouts.default_seconds, 12)
            self.assertEqual(config.runtime.tool_timeouts.actions["wait_time"], 5)
            self.assertEqual(config.runtime.tool_timeouts.actions["spawn_agent"], 90)
            self.assertEqual(
                config.runtime.tool_timeouts.seconds_for(
                    "shell",
                    shell_fallback_seconds=float(config.sandbox.timeout_seconds),
                ),
                77.0,
            )

    def test_openrouter_retry_settings_are_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[llm.openrouter]
max_retries = 4
retry_backoff_seconds = 0.25
empty_response_retries = 3
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.llm.openrouter.max_retries, 4)
            self.assertEqual(config.llm.openrouter.retry_backoff_seconds, 0.25)
            self.assertEqual(config.llm.openrouter.empty_response_retries, 3)

    def test_runtime_tools_list_limit_settings_are_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.tools]
list_default_limit = 80
list_max_limit = 40
shell_inline_wait_seconds = 2.5
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            default_limit, max_limit = config.runtime.tools.list_limit_bounds()
            self.assertEqual(default_limit, 40)
            self.assertEqual(max_limit, 40)
            self.assertEqual(config.runtime.tools.normalize_list_limit(None), 40)
            self.assertEqual(config.runtime.tools.normalize_list_limit(5_000), 40)
            self.assertEqual(config.runtime.tools.shell_inline_wait_seconds, 2.5)

    def test_runtime_tools_shell_inline_wait_seconds_accepts_zero(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.tools]
shell_inline_wait_seconds = 0
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.runtime.tools.shell_inline_wait_seconds, 0.0)

    def test_runtime_tools_steer_agent_scope_is_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.tools]
steer_agent_scope = "descendants"
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.runtime.tools.steer_agent_scope, "descendants")

    def test_runtime_tools_reject_invalid_steer_agent_scope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.tools]
steer_agent_scope = "self"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "steer_agent_scope"):
                OpenCompanyConfig.load(project_dir)

    def test_runtime_root_step_limits_and_reminder_intervals_are_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.limits]
max_root_steps = 12
root_soft_limit_reminder_interval = 0
worker_soft_limit_reminder_interval = -5
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.runtime.limits.max_root_steps, 12)
            self.assertEqual(config.runtime.limits.root_soft_limit_reminder_interval, 1)
            self.assertEqual(config.runtime.limits.worker_soft_limit_reminder_interval, 1)

    def test_runtime_context_defaults_and_overrides_are_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.context]
enabled = true
reminder_ratio = 0.95
keep_pinned_messages = 2
max_context_tokens = 8192
compression_model = "openai/gpt-4o-mini"
overflow_retry_attempts = 3
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            context = config.runtime.context
            self.assertTrue(context.enabled)
            self.assertEqual(context.reminder_ratio, 0.95)
            self.assertEqual(context.keep_pinned_messages, 2)
            self.assertEqual(context.max_context_tokens, 8192)
            self.assertEqual(context.compression_model, "openai/gpt-4o-mini")
            self.assertEqual(context.overflow_retry_attempts, 3)

    def test_runtime_context_overflow_retry_attempts_normalizes_to_non_negative(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.context]
max_context_tokens = 8192
overflow_retry_attempts = -7
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.runtime.context.overflow_retry_attempts, 0)

    def test_runtime_context_requires_max_context_tokens_when_context_section_exists(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.context]
overflow_retry_attempts = 1
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "max_context_tokens is required"):
                OpenCompanyConfig.load(project_dir)

    def test_runtime_limits_reject_legacy_max_root_loops_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[runtime.limits]
max_root_loops = 8
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "max_root_loops has been removed"):
                OpenCompanyConfig.load(project_dir)

    def test_sandbox_rejects_legacy_allow_network(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
allow_network = true
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "allow_network has been removed"):
                OpenCompanyConfig.load(project_dir)

    def test_sandbox_network_policy_allowlist_from_allowed_domains(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
allowed_domains = ["example.com", "openai.com"]
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.sandbox.network_policy, "allowlist")
            self.assertEqual(config.sandbox.allowed_domains, ["example.com", "openai.com"])

    def test_sandbox_allowlist_requires_domains(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
network_policy = "allowlist"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires non-empty"):
                OpenCompanyConfig.load(project_dir)

    def test_sandbox_rejects_invalid_network_policy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
network_policy = "invalid"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "network_policy"):
                OpenCompanyConfig.load(project_dir)

    def test_sandbox_backend_none_is_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[sandbox]
backend = "none"
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)
            self.assertEqual(config.sandbox.backend, "none")

    def test_mcp_config_is_loaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[mcp]
protocol_version = "2025-11-25"

[mcp.servers.filesystem]
transport = "stdio"
enabled = true
title = "Filesystem"
expose_roots = true
timeout_seconds = 45
allowed_tools = ["read_file", "write_file"]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
cwd = "."
env = { API_TOKEN = "env:TEST_TOKEN" }

[mcp.servers.docs]
transport = "streamable_http"
enabled = false
title = "Docs"
url = "http://127.0.0.1:8787/mcp"
headers = { Authorization = "env:DOCS_TOKEN", "X-Test" = "demo" }
oauth_enabled = true
oauth_scopes = ["search", "read"]
oauth_client_name = "Docs Client"
oauth_authorization_prompt = "consent"
oauth_use_resource_param = false
""".strip(),
                encoding="utf-8",
            )
            config = OpenCompanyConfig.load(project_dir)

            self.assertEqual(config.mcp.protocol_version, "2025-11-25")
            self.assertEqual(sorted(config.mcp.servers.keys()), ["docs", "filesystem"])
            filesystem = config.mcp.servers["filesystem"]
            self.assertEqual(filesystem.transport, "stdio")
            self.assertEqual(filesystem.command, "npx")
            self.assertEqual(filesystem.args[-1], ".")
            self.assertEqual(filesystem.allowed_tools, ["read_file", "write_file"])
            self.assertTrue(filesystem.expose_roots)
            self.assertEqual(filesystem.timeout_seconds, 45.0)
            self.assertEqual(filesystem.env["API_TOKEN"], "env:TEST_TOKEN")
            docs = config.mcp.servers["docs"]
            self.assertEqual(docs.transport, "streamable_http")
            self.assertEqual(docs.url, "http://127.0.0.1:8787/mcp")
            self.assertEqual(docs.headers["Authorization"], "env:DOCS_TOKEN")
            self.assertFalse(docs.enabled)
            self.assertTrue(docs.oauth_enabled)
            self.assertEqual(docs.oauth_scopes, ["search", "read"])
            self.assertEqual(docs.oauth_client_name, "Docs Client")
            self.assertEqual(docs.oauth_authorization_prompt, "consent")
            self.assertFalse(docs.oauth_use_resource_param)

    def test_mcp_rejects_invalid_transport_configuration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[mcp.servers.bad]
transport = "streamable_http"
command = "python -m demo"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires non-empty url"):
                OpenCompanyConfig.load(project_dir)

    def test_mcp_rejects_oauth_secret_without_client_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[mcp.servers.docs]
transport = "streamable_http"
url = "https://example.com/mcp"
oauth_enabled = true
oauth_client_secret = "secret"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "oauth_client_secret requires oauth_client_id"):
                OpenCompanyConfig.load(project_dir)

    def test_repository_default_mcp_presets_match_expected_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = OpenCompanyConfig.load(repo_root)
        servers = config.mcp.servers
        preset_ids = {"huggingface", "notion", "github", "duckduckgo"}

        self.assertTrue(preset_ids.issubset(servers))
        self.assertTrue(preset_ids.issubset(set(config.mcp.enabled_server_ids())))

        huggingface = servers["huggingface"]
        self.assertEqual(huggingface.transport, "streamable_http")
        self.assertEqual(huggingface.url, "https://huggingface.co/mcp?login")
        self.assertTrue(huggingface.oauth_enabled)
        self.assertFalse(bool(huggingface.expose_roots))

        notion = servers["notion"]
        self.assertEqual(notion.transport, "streamable_http")
        self.assertEqual(notion.url, "https://mcp.notion.com/mcp")
        self.assertTrue(notion.oauth_enabled)
        self.assertEqual(notion.oauth_authorization_prompt, "consent")
        self.assertFalse(notion.oauth_use_resource_param)
        self.assertFalse(bool(notion.expose_roots))

        github = servers["github"]
        self.assertEqual(github.transport, "streamable_http")
        self.assertEqual(github.url, "https://api.githubcopilot.com/mcp/")
        self.assertFalse(github.oauth_enabled)
        self.assertEqual(
            github.headers.get("Authorization", ""),
            "env:GITHUB_MCP_AUTHORIZATION",
        )
        self.assertFalse(bool(github.expose_roots))

        duckduckgo = servers["duckduckgo"]
        self.assertEqual(duckduckgo.transport, "stdio")
        self.assertEqual(duckduckgo.command, "duckduckgo-mcp-server")
        self.assertEqual(duckduckgo.args, [])
        self.assertEqual(duckduckgo.env.get("DDG_SAFE_SEARCH", ""), "MODERATE")
        self.assertEqual(duckduckgo.env.get("DDG_REGION", ""), "wt-wt")
        self.assertFalse(bool(duckduckgo.expose_roots))
