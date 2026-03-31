from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencompany.utils import detect_system_locale

NETWORK_POLICIES = frozenset({"deny_all", "allow_all", "allowlist"})
STEER_AGENT_SCOPES = frozenset({"session", "descendants"})
MCP_TRANSPORTS = frozenset({"stdio", "streamable_http"})
MCP_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
)


@dataclass(slots=True)
class ProjectConfig:
    name: str = "OpenCompany"
    default_locale: str = "auto"
    data_dir: str = ".opencompany"


@dataclass(slots=True)
class OpenRouterConfig:
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "qwen/qwen3.6-plus-preview:free"
    coordinator_model: str = ""
    worker_model: str = ""
    timeout_seconds: int = 120
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    empty_response_retries: int = 1
    max_tokens: int = 4000
    temperature: float = 0.2

    @property
    def api_key(self) -> str | None:
        return os.environ.get("OPENROUTER_API_KEY")

    def model_for_role(self, role: str) -> str:
        if role == "root" and self.coordinator_model:
            return self.coordinator_model
        if role == "worker" and self.worker_model:
            return self.worker_model
        return self.model


@dataclass(slots=True)
class RuntimeLimitsConfig:
    max_children_per_agent: int = 3
    max_active_agents: int = 3
    max_root_steps: int = 3
    max_agent_steps: int = 8
    root_soft_limit_reminder_interval: int = 1
    worker_soft_limit_reminder_interval: int = 2


@dataclass(slots=True)
class ToolTimeoutsConfig:
    default_seconds: float = 20.0
    shell_seconds: float = 0.0
    actions: dict[str, float] = field(
        default_factory=lambda: {
            "compress_context": 180.0,
            "wait_time": 0.0,
            "list_mcp_servers": 0.0,
            "list_mcp_resources": 0.0,
            "read_mcp_resource": 0.0,
            "mcp_tool": 30.0,
            "list_agent_runs": 0.0,
            "get_agent_run": 0.0,
            "cancel_agent": 0.0,
            "steer_agent": 0.0,
            "spawn_agent": 0.0,
            "wait_run": 0.0,
            "cancel_tool_run": 0.0,
        }
    )

    def seconds_for(self, action_type: str, *, shell_fallback_seconds: float) -> float:
        if action_type == "shell":
            return self._normalized(self.shell_seconds, fallback=shell_fallback_seconds)
        action_specific = self.actions.get(action_type, self.default_seconds)
        return self._normalized(action_specific, fallback=self.default_seconds)

    @staticmethod
    def _normalized(value: float, *, fallback: float) -> float:
        if value > 0:
            return float(value)
        if fallback > 0:
            return float(fallback)
        return 1.0


@dataclass(slots=True)
class RuntimeConfig:
    limits: RuntimeLimitsConfig = field(default_factory=RuntimeLimitsConfig)
    tool_timeouts: ToolTimeoutsConfig = field(default_factory=ToolTimeoutsConfig)
    context: "RuntimeContextConfig" = field(default_factory=lambda: RuntimeContextConfig())
    tools: "RuntimeToolsConfig" = field(default_factory=lambda: RuntimeToolsConfig())


@dataclass(slots=True)
class RuntimeContextConfig:
    enabled: bool = True
    reminder_ratio: float = 0.8
    keep_pinned_messages: int = 1
    max_context_tokens: int = 128_000
    compression_model: str = ""
    overflow_retry_attempts: int = 1

    @staticmethod
    def normalize_reminder_ratio(value: Any, *, fallback: float = 0.8) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(fallback)
        if numeric <= 0:
            return float(fallback)
        if numeric > 1:
            return 1.0
        return numeric

    @staticmethod
    def normalize_keep_pinned_messages(value: Any, *, fallback: int = 1) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = int(fallback)
        return max(0, numeric)

    @staticmethod
    def normalize_max_context_tokens(value: Any, *, fallback: int) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = int(fallback)
        if numeric <= 0:
            raise ValueError("[runtime.context].max_context_tokens must be > 0.")
        return numeric

    @staticmethod
    def normalize_overflow_retry_attempts(value: Any, *, fallback: int = 1) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = int(fallback)
        return max(0, numeric)


@dataclass(slots=True)
class RuntimeToolsConfig:
    root_tools: list[str] = field(
        default_factory=lambda: [
            "shell",
            "compress_context",
            "wait_time",
            "list_mcp_servers",
            "list_mcp_resources",
            "read_mcp_resource",
            "list_agent_runs",
            "get_agent_run",
            "spawn_agent",
            "cancel_agent",
            "steer_agent",
            "list_tool_runs",
            "get_tool_run",
            "wait_run",
            "cancel_tool_run",
            "finish",
        ]
    )
    worker_tools: list[str] = field(
        default_factory=lambda: [
            "shell",
            "compress_context",
            "wait_time",
            "list_mcp_servers",
            "list_mcp_resources",
            "read_mcp_resource",
            "list_agent_runs",
            "get_agent_run",
            "spawn_agent",
            "cancel_agent",
            "steer_agent",
            "list_tool_runs",
            "get_tool_run",
            "wait_run",
            "cancel_tool_run",
            "finish",
        ]
    )
    steer_agent_scope: str = "session"
    list_default_limit: int = 20
    list_max_limit: int = 200
    shell_inline_wait_seconds: float = 5.0
    wait_time_min_seconds: float = 10.0
    wait_time_max_seconds: float = 60.0

    def tool_names_for_role(self, role: str) -> list[str]:
        if role == "root":
            return list(self.root_tools)
        return list(self.worker_tools)

    def list_limit_bounds(self) -> tuple[int, int]:
        default_limit = self._coerce_positive_int(self.list_default_limit, fallback=20)
        max_limit = self._coerce_positive_int(self.list_max_limit, fallback=200)
        if default_limit > max_limit:
            default_limit = max_limit
        return default_limit, max_limit

    def normalize_list_limit(self, value: Any | None) -> int:
        default_limit, max_limit = self.list_limit_bounds()
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = default_limit
        return max(1, min(max_limit, numeric))

    def wait_time_bounds(self) -> tuple[float, float]:
        minimum = self.normalize_wait_time_bound_seconds(
            self.wait_time_min_seconds,
            fallback=10.0,
        )
        maximum = self.normalize_wait_time_bound_seconds(
            self.wait_time_max_seconds,
            fallback=60.0,
        )
        if maximum < minimum:
            maximum = minimum
        return minimum, maximum

    @staticmethod
    def normalize_shell_inline_wait_seconds(value: Any, *, fallback: float = 5.0) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(fallback)
        if numeric < 0:
            return max(0.0, float(fallback))
        return numeric

    @staticmethod
    def normalize_steer_agent_scope(value: Any, *, fallback: str = "session") -> str:
        normalized = str(value or "").strip().lower() or fallback
        if normalized not in STEER_AGENT_SCOPES:
            allowed = ", ".join(sorted(STEER_AGENT_SCOPES))
            raise ValueError(
                "[runtime.tools].steer_agent_scope must be one of: " + allowed + "."
            )
        return normalized

    @staticmethod
    def normalize_wait_time_bound_seconds(value: Any, *, fallback: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(fallback)
        if numeric <= 0:
            numeric = float(fallback)
        if numeric <= 0:
            return 1.0
        return float(numeric)

    @staticmethod
    def _coerce_positive_int(value: Any, *, fallback: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(fallback))


@dataclass(slots=True)
class SandboxConfig:
    backend: str = "anthropic"
    cli_path: str = ""
    network_policy: str = "deny_all"
    allowed_domains: list[str] = field(default_factory=list)
    timeout_seconds: int = 300


@dataclass(slots=True)
class LoggingConfig:
    jsonl_filename: str = "events.jsonl"
    export_filename: str = "export.json"
    diagnostics_filename: str = "diagnostics.jsonl"


@dataclass(slots=True)
class LocaleConfig:
    fallback: str = "en"


@dataclass(slots=True)
class LlmConfig:
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)


@dataclass(slots=True)
class McpServerConfig:
    id: str = ""
    transport: str = "stdio"
    enabled: bool = True
    title: str = ""
    expose_roots: bool | None = None
    timeout_seconds: float = 30.0
    allowed_tools: list[str] = field(default_factory=list)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    oauth_enabled: bool = False
    oauth_scopes: list[str] = field(default_factory=list)
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_client_name: str = "OpenCompany MCP Client"
    oauth_client_uri: str = ""
    oauth_authorization_prompt: str = ""
    oauth_use_resource_param: bool = True

    def resolved_title(self) -> str:
        return str(self.title or self.id).strip() or str(self.id or "").strip()


@dataclass(slots=True)
class McpConfig:
    protocol_version: str = MCP_PROTOCOL_VERSIONS[0]
    servers: dict[str, McpServerConfig] = field(default_factory=dict)

    def enabled_server_ids(self) -> list[str]:
        return [
            server_id
            for server_id, server in sorted(self.servers.items())
            if server.enabled
        ]


@dataclass(slots=True)
class OpenCompanyConfig:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    locale: LocaleConfig = field(default_factory=LocaleConfig)

    @classmethod
    def load(cls, app_dir: Path) -> "OpenCompanyConfig":
        config = cls()
        config_path = app_dir / "opencompany.toml"
        if config_path.exists():
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            config._merge(data)
        return config

    def _merge(self, data: dict) -> None:
        project = data.get("project", {})
        self.project = ProjectConfig(
            name=project.get("name", self.project.name),
            default_locale=project.get("default_locale", self.project.default_locale),
            data_dir=project.get("data_dir", self.project.data_dir),
        )

        openrouter = data.get("llm", {}).get("openrouter", {})
        self.llm = LlmConfig(
            openrouter=OpenRouterConfig(
                base_url=openrouter.get("base_url", self.llm.openrouter.base_url),
                model=openrouter.get("model", self.llm.openrouter.model),
                coordinator_model=openrouter.get(
                    "coordinator_model", self.llm.openrouter.coordinator_model
                ),
                worker_model=openrouter.get("worker_model", self.llm.openrouter.worker_model),
                timeout_seconds=int(
                    openrouter.get("timeout_seconds", self.llm.openrouter.timeout_seconds)
                ),
                max_retries=int(
                    openrouter.get("max_retries", self.llm.openrouter.max_retries)
                ),
                retry_backoff_seconds=float(
                    openrouter.get(
                        "retry_backoff_seconds",
                        self.llm.openrouter.retry_backoff_seconds,
                    )
                ),
                empty_response_retries=int(
                    openrouter.get(
                        "empty_response_retries",
                        self.llm.openrouter.empty_response_retries,
                    )
                ),
                max_tokens=int(openrouter.get("max_tokens", self.llm.openrouter.max_tokens)),
                temperature=float(
                    openrouter.get("temperature", self.llm.openrouter.temperature)
                ),
            )
        )

        limits = data.get("runtime", {}).get("limits", {})
        if isinstance(limits, dict) and "max_root_loops" in limits:
            raise ValueError(
                "[runtime.limits].max_root_loops has been removed. "
                "Use [runtime.limits].max_root_steps instead."
            )
        context = data.get("runtime", {}).get("context", {})
        if isinstance(context, dict) and context and "max_context_tokens" not in context:
            raise ValueError("[runtime.context].max_context_tokens is required.")
        tool_timeouts = data.get("runtime", {}).get("tool_timeouts", {})
        tools = data.get("runtime", {}).get("tools", {})
        timeout_actions = dict(self.runtime.tool_timeouts.actions)
        actions_table = tool_timeouts.get("actions", {})
        if isinstance(actions_table, dict):
            for action_name, raw_seconds in actions_table.items():
                normalized_action_name = str(action_name).strip()
                if not normalized_action_name:
                    continue
                timeout_actions[normalized_action_name] = float(raw_seconds)
        # Backward-compatible parser for flat "<tool>_seconds" keys in TOML.
        for key, raw_seconds in tool_timeouts.items():
            normalized_key = str(key).strip()
            if (
                normalized_key in {"default_seconds", "shell_seconds", "actions"}
                or not normalized_key.endswith("_seconds")
            ):
                continue
            action_name = normalized_key[: -len("_seconds")].strip()
            if not action_name:
                continue
            timeout_actions[action_name] = float(raw_seconds)
        root_tools = tools.get("root_tools", self.runtime.tools.root_tools)
        worker_tools = tools.get("worker_tools", self.runtime.tools.worker_tools)
        steer_agent_scope = tools.get(
            "steer_agent_scope",
            self.runtime.tools.steer_agent_scope,
        )
        list_default_limit = tools.get(
            "list_default_limit",
            self.runtime.tools.list_default_limit,
        )
        list_max_limit = tools.get(
            "list_max_limit",
            self.runtime.tools.list_max_limit,
        )
        shell_inline_wait_seconds = tools.get(
            "shell_inline_wait_seconds",
            self.runtime.tools.shell_inline_wait_seconds,
        )
        wait_time_min_seconds = tools.get(
            "wait_time_min_seconds",
            self.runtime.tools.wait_time_min_seconds,
        )
        wait_time_max_seconds = tools.get(
            "wait_time_max_seconds",
            self.runtime.tools.wait_time_max_seconds,
        )
        normalized_list_default = RuntimeToolsConfig._coerce_positive_int(
            list_default_limit,
            fallback=self.runtime.tools.list_default_limit,
        )
        normalized_list_max = RuntimeToolsConfig._coerce_positive_int(
            list_max_limit,
            fallback=self.runtime.tools.list_max_limit,
        )
        normalized_steer_agent_scope = RuntimeToolsConfig.normalize_steer_agent_scope(
            steer_agent_scope,
            fallback=self.runtime.tools.steer_agent_scope,
        )
        normalized_shell_inline_wait_seconds = RuntimeToolsConfig.normalize_shell_inline_wait_seconds(
            shell_inline_wait_seconds,
            fallback=self.runtime.tools.shell_inline_wait_seconds,
        )
        normalized_wait_time_min_seconds = RuntimeToolsConfig.normalize_wait_time_bound_seconds(
            wait_time_min_seconds,
            fallback=self.runtime.tools.wait_time_min_seconds,
        )
        normalized_wait_time_max_seconds = RuntimeToolsConfig.normalize_wait_time_bound_seconds(
            wait_time_max_seconds,
            fallback=self.runtime.tools.wait_time_max_seconds,
        )
        if normalized_wait_time_max_seconds < normalized_wait_time_min_seconds:
            normalized_wait_time_max_seconds = normalized_wait_time_min_seconds
        normalized_context = RuntimeContextConfig(
            enabled=bool(context.get("enabled", self.runtime.context.enabled)),
            reminder_ratio=RuntimeContextConfig.normalize_reminder_ratio(
                context.get("reminder_ratio", self.runtime.context.reminder_ratio),
                fallback=self.runtime.context.reminder_ratio,
            ),
            keep_pinned_messages=RuntimeContextConfig.normalize_keep_pinned_messages(
                context.get("keep_pinned_messages", self.runtime.context.keep_pinned_messages),
                fallback=self.runtime.context.keep_pinned_messages,
            ),
            max_context_tokens=RuntimeContextConfig.normalize_max_context_tokens(
                context.get("max_context_tokens", self.runtime.context.max_context_tokens),
                fallback=self.runtime.context.max_context_tokens,
            ),
            compression_model=str(
                context.get("compression_model", self.runtime.context.compression_model)
            ).strip(),
            overflow_retry_attempts=RuntimeContextConfig.normalize_overflow_retry_attempts(
                context.get("overflow_retry_attempts", self.runtime.context.overflow_retry_attempts),
                fallback=self.runtime.context.overflow_retry_attempts,
            ),
        )
        self.runtime = RuntimeConfig(
            limits=RuntimeLimitsConfig(
                max_children_per_agent=int(
                    limits.get(
                        "max_children_per_agent",
                        self.runtime.limits.max_children_per_agent,
                    )
                ),
                max_active_agents=int(
                    limits.get("max_active_agents", self.runtime.limits.max_active_agents)
                ),
                max_root_steps=int(
                    limits.get("max_root_steps", self.runtime.limits.max_root_steps)
                ),
                max_agent_steps=int(
                    limits.get("max_agent_steps", self.runtime.limits.max_agent_steps)
                ),
                root_soft_limit_reminder_interval=max(
                    1,
                    int(
                        limits.get(
                            "root_soft_limit_reminder_interval",
                            self.runtime.limits.root_soft_limit_reminder_interval,
                        )
                    ),
                ),
                worker_soft_limit_reminder_interval=max(
                    1,
                    int(
                        limits.get(
                            "worker_soft_limit_reminder_interval",
                            self.runtime.limits.worker_soft_limit_reminder_interval,
                        )
                    ),
                ),
            ),
            tool_timeouts=ToolTimeoutsConfig(
                default_seconds=float(
                    tool_timeouts.get(
                        "default_seconds",
                        self.runtime.tool_timeouts.default_seconds,
                    )
                ),
                shell_seconds=float(
                    tool_timeouts.get(
                        "shell_seconds",
                        self.runtime.tool_timeouts.shell_seconds,
                    )
                ),
                actions={
                    str(action_name).strip(): float(raw_seconds)
                    for action_name, raw_seconds in timeout_actions.items()
                    if str(action_name).strip()
                },
            ),
            context=normalized_context,
            tools=RuntimeToolsConfig(
                root_tools=[
                    str(name).strip()
                    for name in root_tools
                    if str(name).strip()
                ],
                worker_tools=[
                    str(name).strip()
                    for name in worker_tools
                    if str(name).strip()
                ],
                steer_agent_scope=normalized_steer_agent_scope,
                list_default_limit=normalized_list_default,
                list_max_limit=normalized_list_max,
                shell_inline_wait_seconds=normalized_shell_inline_wait_seconds,
                wait_time_min_seconds=normalized_wait_time_min_seconds,
                wait_time_max_seconds=normalized_wait_time_max_seconds,
            ),
        )

        sandbox = data.get("sandbox", {})
        if isinstance(sandbox, dict) and "allow_network" in sandbox:
            raise ValueError(
                "[sandbox].allow_network has been removed. "
                "Use [sandbox].network_policy with one of: deny_all, allow_all, allowlist."
            )
        raw_policy = sandbox.get("network_policy")
        policy = str(raw_policy).strip().lower() if raw_policy is not None else ""
        raw_allowed_domains = sandbox.get("allowed_domains", self.sandbox.allowed_domains)
        allowed_domains: list[str] = []
        if isinstance(raw_allowed_domains, (list, tuple, set)):
            for item in raw_allowed_domains:
                normalized_domain = str(item).strip()
                if normalized_domain:
                    allowed_domains.append(normalized_domain)
        if not policy:
            if allowed_domains:
                policy = "allowlist"
            else:
                policy = "deny_all"
        if policy not in NETWORK_POLICIES:
            supported = ", ".join(sorted(NETWORK_POLICIES))
            raise ValueError(
                f"[sandbox].network_policy '{policy}' is invalid. Supported: {supported}."
            )
        if policy == "allowlist":
            if not allowed_domains:
                raise ValueError(
                    "[sandbox].network_policy='allowlist' requires non-empty [sandbox].allowed_domains."
                )
        else:
            allowed_domains = []

        self.sandbox = SandboxConfig(
            backend=sandbox.get("backend", self.sandbox.backend),
            cli_path=sandbox.get("cli_path", self.sandbox.cli_path),
            network_policy=policy,
            allowed_domains=allowed_domains,
            timeout_seconds=int(
                sandbox.get("timeout_seconds", self.sandbox.timeout_seconds)
            ),
        )

        mcp = data.get("mcp", {})
        protocol_version = str(
            mcp.get("protocol_version", self.mcp.protocol_version)
        ).strip() or self.mcp.protocol_version
        if protocol_version not in MCP_PROTOCOL_VERSIONS:
            supported = ", ".join(MCP_PROTOCOL_VERSIONS)
            raise ValueError(
                f"[mcp].protocol_version '{protocol_version}' is invalid. Supported: {supported}."
            )
        raw_servers = mcp.get("servers", {})
        normalized_servers: dict[str, McpServerConfig] = {}
        if raw_servers:
            if not isinstance(raw_servers, dict):
                raise ValueError("[mcp].servers must be a table of named server configs.")
            for raw_server_id, raw_server in raw_servers.items():
                server_id = str(raw_server_id).strip()
                if not server_id:
                    continue
                if not isinstance(raw_server, dict):
                    raise ValueError(f"[mcp.servers.{server_id}] must be a table.")
                transport = str(
                    raw_server.get("transport", "stdio")
                ).strip().lower() or "stdio"
                if transport not in MCP_TRANSPORTS:
                    supported = ", ".join(sorted(MCP_TRANSPORTS))
                    raise ValueError(
                        f"[mcp.servers.{server_id}].transport '{transport}' is invalid. Supported: {supported}."
                    )
                args = raw_server.get("args", [])
                if args is None:
                    args = []
                if not isinstance(args, (list, tuple)):
                    raise ValueError(f"[mcp.servers.{server_id}].args must be a list.")
                env_table = raw_server.get("env", {})
                if env_table is None:
                    env_table = {}
                if not isinstance(env_table, dict):
                    raise ValueError(f"[mcp.servers.{server_id}].env must be a table.")
                headers_table = raw_server.get("headers", {})
                if headers_table is None:
                    headers_table = {}
                if not isinstance(headers_table, dict):
                    raise ValueError(f"[mcp.servers.{server_id}].headers must be a table.")
                oauth_scopes = raw_server.get("oauth_scopes", [])
                if oauth_scopes is None:
                    oauth_scopes = []
                if not isinstance(oauth_scopes, (list, tuple)):
                    raise ValueError(
                        f"[mcp.servers.{server_id}].oauth_scopes must be a list."
                    )
                allowed_tools = raw_server.get("allowed_tools", [])
                if allowed_tools is None:
                    allowed_tools = []
                if not isinstance(allowed_tools, (list, tuple)):
                    raise ValueError(
                        f"[mcp.servers.{server_id}].allowed_tools must be a list."
                    )
                command = str(raw_server.get("command", "") or "").strip()
                url = str(raw_server.get("url", "") or "").strip()
                oauth_enabled = bool(raw_server.get("oauth_enabled", False))
                oauth_client_id = str(raw_server.get("oauth_client_id", "") or "").strip()
                oauth_client_secret = str(
                    raw_server.get("oauth_client_secret", "") or ""
                ).strip()
                oauth_client_name = str(
                    raw_server.get("oauth_client_name", "OpenCompany MCP Client") or ""
                ).strip() or "OpenCompany MCP Client"
                oauth_client_uri = str(
                    raw_server.get("oauth_client_uri", "") or ""
                ).strip()
                oauth_authorization_prompt = str(
                    raw_server.get("oauth_authorization_prompt", "") or ""
                ).strip()
                oauth_use_resource_param = bool(raw_server.get("oauth_use_resource_param", True))
                if transport == "stdio":
                    if not command:
                        raise ValueError(
                            f"[mcp.servers.{server_id}] transport='stdio' requires non-empty command."
                        )
                    if url:
                        raise ValueError(
                            f"[mcp.servers.{server_id}] transport='stdio' must not set url."
                        )
                    if oauth_enabled:
                        raise ValueError(
                            f"[mcp.servers.{server_id}] OAuth is only supported with transport='streamable_http'."
                        )
                if transport == "streamable_http":
                    if not url:
                        raise ValueError(
                            f"[mcp.servers.{server_id}] transport='streamable_http' requires non-empty url."
                        )
                    if command:
                        raise ValueError(
                            f"[mcp.servers.{server_id}] transport='streamable_http' must not set command."
                        )
                if oauth_client_secret and not oauth_client_id:
                    raise ValueError(
                        f"[mcp.servers.{server_id}] oauth_client_secret requires oauth_client_id."
                    )
                normalized_servers[server_id] = McpServerConfig(
                    id=server_id,
                    transport=transport,
                    enabled=bool(raw_server.get("enabled", True)),
                    title=str(raw_server.get("title", "") or "").strip(),
                    expose_roots=(
                        None
                        if raw_server.get("expose_roots") is None
                        else bool(raw_server.get("expose_roots"))
                    ),
                    timeout_seconds=float(
                        raw_server.get("timeout_seconds", 30.0)
                    ),
                    allowed_tools=[
                        str(item).strip()
                        for item in allowed_tools
                        if str(item).strip()
                    ],
                    command=command,
                    args=[str(item) for item in args],
                    env={
                        str(key).strip(): str(value)
                        for key, value in env_table.items()
                        if str(key).strip()
                    },
                    cwd=str(raw_server.get("cwd", "") or "").strip(),
                    url=url,
                    headers={
                        str(key).strip(): str(value)
                        for key, value in headers_table.items()
                        if str(key).strip()
                    },
                    oauth_enabled=oauth_enabled,
                    oauth_scopes=[
                        str(item).strip()
                        for item in oauth_scopes
                        if str(item).strip()
                    ],
                    oauth_client_id=oauth_client_id,
                    oauth_client_secret=oauth_client_secret,
                    oauth_client_name=oauth_client_name,
                    oauth_client_uri=oauth_client_uri,
                    oauth_authorization_prompt=oauth_authorization_prompt,
                    oauth_use_resource_param=oauth_use_resource_param,
                )
        self.mcp = McpConfig(
            protocol_version=protocol_version,
            servers=normalized_servers,
        )

        logging = data.get("logging", {})
        self.logging = LoggingConfig(
            jsonl_filename=logging.get("jsonl_filename", self.logging.jsonl_filename),
            export_filename=logging.get("export_filename", self.logging.export_filename),
            diagnostics_filename=logging.get(
                "diagnostics_filename",
                self.logging.diagnostics_filename,
            ),
        )

        locale_cfg = data.get("locale", {})
        self.locale = LocaleConfig(
            fallback=locale_cfg.get("fallback", self.locale.fallback)
        )

    def resolve_locale(self, requested_locale: str | None = None) -> str:
        if requested_locale in {"en", "zh"}:
            return requested_locale
        if self.project.default_locale in {"en", "zh"}:
            return self.project.default_locale
        if self.project.default_locale == "auto":
            return detect_system_locale()
        return self.locale.fallback
