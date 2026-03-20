from __future__ import annotations

import asyncio
import contextlib
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from opencompany.config import McpServerConfig, OpenCompanyConfig
from opencompany.mcp.models import (
    MCP_MAX_INLINE_BINARY_BYTES,
    MCP_MAX_LIST_ITEMS,
    McpResourceDescriptor,
    McpServerRuntimeState,
    McpToolDescriptor,
    expand_header_value,
    filter_allowed_tools,
    should_expose_roots,
    synthetic_tool_name,
    truncate_text_payload,
)
from opencompany.mcp.oauth import McpOAuthStore, McpOAuthTokenProvider
from opencompany.mcp.session import (
    LegacySseMcpTransport,
    McpClientSession,
    McpError,
    McpRequestError,
    StdioMcpTransport,
    StreamableHttpMcpTransport,
    derive_legacy_sse_url,
    normalize_streamable_http_url,
)
from opencompany.models import AgentNode, AgentRole, RunSession
from opencompany.paths import RuntimePaths
from opencompany.utils import utc_now

if TYPE_CHECKING:
    from opencompany.tools.executor import ToolExecutor


DiagnosticLoggerFn = Any


def render_mcp_prompt(locale: str, state: dict[str, Any] | None) -> str:
    normalized_state = state if isinstance(state, dict) else {}
    enabled_ids = [
        str(item).strip()
        for item in normalized_state.get("enabled_server_ids", [])
        if str(item).strip()
    ]
    entries = [
        item
        for item in normalized_state.get("entries", [])
        if isinstance(item, dict)
    ]
    if not enabled_ids and not entries:
        return ""
    lines = [
        "Enabled MCP Servers"
        if locale != "zh"
        else "已启用的 MCP Servers",
    ]
    if enabled_ids:
        lines.append(
            ("Enabled ids: " if locale != "zh" else "启用 ID：")
            + ", ".join(enabled_ids)
        )
    for entry in entries:
        title = str(entry.get("title", "") or entry.get("id", "")).strip()
        server_id = str(entry.get("id", "")).strip()
        transport = str(entry.get("transport", "")).strip()
        connected = bool(entry.get("connected", False))
        roots_enabled = bool(entry.get("roots_enabled", False))
        tool_count = int(entry.get("tool_count", 0) or 0)
        resource_count = int(entry.get("resource_count", 0) or 0)
        warning = str(entry.get("warning", "")).strip()
        lines.append(
            (
                f"- {title} ({server_id}) [{transport}] connected={str(connected).lower()} "
                f"roots={str(roots_enabled).lower()} tools={tool_count} resources={resource_count}"
            )
            if locale != "zh"
            else (
                f"- {title} ({server_id}) [{transport}] 已连接={str(connected).lower()} "
                f"roots={str(roots_enabled).lower()} tools={tool_count} resources={resource_count}"
            )
        )
        if warning:
            lines.append(
                ("  warning: " if locale != "zh" else "  警告：") + warning
            )
    return "\n".join(lines).strip()


@dataclass(slots=True)
class _AgentServerContext:
    server: McpServerConfig
    runtime_state: McpServerRuntimeState
    session: McpClientSession | None = None
    tool_descriptors: list[McpToolDescriptor] = field(default_factory=list)
    tool_by_synthetic_name: dict[str, McpToolDescriptor] = field(default_factory=dict)
    resources: list[McpResourceDescriptor] = field(default_factory=list)


@dataclass(slots=True)
class _AgentMcpContext:
    session_id: str
    agent_id: str
    workspace_path: Path
    workspace_is_remote: bool
    enabled_server_ids: list[str]
    tool_executor: Any | None = None
    servers: dict[str, _AgentServerContext] = field(default_factory=dict)

    @staticmethod
    def _distinct_strings(values: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    def metadata_payload(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for _server_id, ctx in sorted(self.servers.items()):
            entry = ctx.runtime_state.to_dict()
            tool_names = self._distinct_strings(
                [descriptor.tool_name for descriptor in ctx.tool_descriptors]
            )
            tool_items = [
                {
                    "tool_name": descriptor.tool_name,
                    "title": descriptor.title,
                    "description": descriptor.description,
                    "synthetic_name": descriptor.synthetic_name,
                }
                for descriptor in ctx.tool_descriptors
            ]
            resource_uris = self._distinct_strings([resource.uri for resource in ctx.resources])
            resource_names = self._distinct_strings(
                [resource.title or resource.name or resource.uri for resource in ctx.resources]
            )
            resource_items = [
                {
                    "uri": resource.uri,
                    "name": resource.name,
                    "title": resource.title,
                    "description": resource.description,
                    "mime_type": resource.mime_type,
                }
                for resource in ctx.resources
            ]
            entry["tool_names"] = tool_names
            entry["tool_items"] = tool_items
            entry["resource_uris"] = resource_uris
            entry["resource_names"] = resource_names
            entry["resource_items"] = resource_items
            entries.append(entry)
        warnings = [
            {
                "server_id": server_id,
                "message": ctx.runtime_state.warning,
            }
            for server_id, ctx in self.servers.items()
            if ctx.runtime_state.warning
        ]
        dynamic_tools = [
            descriptor.to_tool_definition()
            for ctx in self.servers.values()
            for descriptor in ctx.tool_descriptors
        ]
        return {
            "enabled": bool(self.enabled_server_ids),
            "enabled_server_ids": list(self.enabled_server_ids),
            "entries": entries,
            "warnings": warnings,
            "dynamic_tools": dynamic_tools,
            "updated_at": utc_now(),
        }


@dataclass(slots=True)
class _SessionMcpContext:
    session_id: str
    workspace_path: Path
    workspace_is_remote: bool
    enabled_server_ids: list[str]
    servers: dict[str, _AgentServerContext] = field(default_factory=dict)


class McpManager:
    def __init__(
        self,
        *,
        app_dir: Path,
        config: OpenCompanyConfig,
        log_diagnostic: DiagnosticLoggerFn,
    ) -> None:
        self.app_dir = app_dir
        self.config = config
        self._log_diagnostic = log_diagnostic
        self._agent_contexts: dict[tuple[str, str], _AgentMcpContext] = {}
        self._session_contexts: dict[str, _SessionMcpContext] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    def available_servers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for server_id, server in sorted(self.config.mcp.servers.items()):
            rows.append(
                {
                    "id": server_id,
                    "title": server.resolved_title(),
                    "transport": server.transport,
                    "enabled": server.enabled,
                    "expose_roots": server.expose_roots,
                    "timeout_seconds": float(server.timeout_seconds),
                    "allowed_tools": list(server.allowed_tools),
                    "cwd": str(server.cwd or ""),
                    "command": str(server.command or ""),
                    "args": list(server.args),
                    "url": str(server.url or ""),
                    "oauth_enabled": bool(server.oauth_enabled),
                    "oauth_scopes": list(server.oauth_scopes),
                }
            )
        return rows

    def normalize_enabled_server_ids(self, requested_ids: list[str] | None) -> list[str]:
        if requested_ids is None:
            return list(self.config.mcp.enabled_server_ids())
        seen: set[str] = set()
        normalized: list[str] = []
        for item in requested_ids:
            server_id = str(item or "").strip()
            if not server_id or server_id in seen:
                continue
            seen.add(server_id)
            normalized.append(server_id)
        return normalized

    def session_state(
        self,
        *,
        enabled_server_ids: list[str],
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for server_id in enabled_server_ids:
            server = self.config.mcp.servers.get(server_id)
            if server is None:
                warnings.append(
                    {
                        "server_id": server_id,
                        "message": f"MCP server '{server_id}' is not defined in opencompany.toml.",
                    }
                )
                continue
            entries.append(
                McpServerRuntimeState(
                    server_id=server_id,
                    title=server.resolved_title(),
                    transport=server.transport,
                    enabled=True,
                ).to_dict()
            )
        return {
            "enabled": bool(enabled_server_ids),
            "enabled_server_ids": list(enabled_server_ids),
            "entries": entries,
            "warnings": warnings,
            "updated_at": utc_now(),
        }

    async def inspect_servers(
        self,
        *,
        enabled_server_ids: list[str] | None,
        workspace_path: Path,
        workspace_is_remote: bool,
        tool_executor: "ToolExecutor",
        session_id: str,
    ) -> list[dict[str, Any]]:
        fake_agent = AgentNode(
            id="agent-mcp-inspect",
            session_id=session_id,
            name="MCP Inspect",
            role=AgentRole.ROOT,
            instruction="Inspect MCP servers",
            workspace_id="workspace-mcp-inspect",
        )
        fake_session = RunSession(
            id=session_id,
            project_dir=workspace_path,
            task="Inspect MCP servers",
            locale="en",
            root_agent_id=fake_agent.id,
            enabled_mcp_server_ids=self.normalize_enabled_server_ids(enabled_server_ids),
        )
        try:
            await self.prepare_agent(
                session=fake_session,
                agent=fake_agent,
                workspace_path=workspace_path,
                workspace_is_remote=workspace_is_remote,
                tool_executor=tool_executor,
            )
            context = self._require_context(session_id=session_id, agent_id=fake_agent.id)
            rows: list[dict[str, Any]] = []
            for server_id, server_context in sorted(context.servers.items()):
                rows.append(
                    {
                        **server_context.runtime_state.to_dict(),
                        "server_id": server_id,
                        "tools": [
                            {
                                "synthetic_name": descriptor.synthetic_name,
                                "tool_name": descriptor.tool_name,
                                "title": descriptor.title,
                                "description": descriptor.description,
                            }
                            for descriptor in server_context.tool_descriptors
                        ],
                        "resources": [
                            {
                                "uri": resource.uri,
                                "name": resource.name,
                                "title": resource.title,
                                "description": resource.description,
                                "mime_type": resource.mime_type,
                            }
                            for resource in server_context.resources
                        ],
                    }
                )
            return rows
        finally:
            await self.close_agent(session_id=session_id, agent_id=fake_agent.id)

    async def prepare_agent(
        self,
        *,
        session: RunSession,
        agent: AgentNode,
        workspace_path: Path,
        workspace_is_remote: bool,
        tool_executor: "ToolExecutor",
    ) -> dict[str, Any]:
        enabled_server_ids = self.normalize_enabled_server_ids(session.enabled_mcp_server_ids)
        lock = self._session_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            session_context = self._session_contexts.get(session.id)
            if (
                session_context is None
                or session_context.workspace_path != workspace_path
                or session_context.workspace_is_remote != workspace_is_remote
                or session_context.enabled_server_ids != enabled_server_ids
            ):
                if session_context is not None:
                    await self._close_session_context(
                        session_context,
                        session_id=session.id,
                        agent_id=agent.id,
                    )
                session_context = _SessionMcpContext(
                    session_id=session.id,
                    workspace_path=workspace_path,
                    workspace_is_remote=workspace_is_remote,
                    enabled_server_ids=list(enabled_server_ids),
                )
                self._session_contexts[session.id] = session_context

            context_key = (session.id, agent.id)
            context = self._agent_contexts.get(context_key)
            if context is None:
                context = _AgentMcpContext(
                    session_id=session.id,
                    agent_id=agent.id,
                    workspace_path=workspace_path,
                    workspace_is_remote=workspace_is_remote,
                    enabled_server_ids=list(enabled_server_ids),
                    tool_executor=tool_executor,
                    servers=session_context.servers,
                )
                self._agent_contexts[context_key] = context
            else:
                context.workspace_path = workspace_path
                context.workspace_is_remote = workspace_is_remote
                context.enabled_server_ids = list(enabled_server_ids)
                context.tool_executor = tool_executor
                context.servers = session_context.servers

            await self._ensure_servers(
                context=context,
                tool_executor=tool_executor,
            )
            return context.metadata_payload()

    async def list_servers(
        self,
        *,
        session_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        context = self._require_context(session_id=session_id, agent_id=agent_id)
        entries = [ctx.runtime_state.to_dict() for ctx in context.servers.values()]
        return {
            "mcp_servers_count": len(entries),
            "mcp_servers": entries,
        }

    async def list_resources(
        self,
        *,
        session_id: str,
        agent_id: str,
        server_id: str | None,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        context = self._require_context(session_id=session_id, agent_id=agent_id)
        resources: list[dict[str, Any]] = []
        normalized_server_id = str(server_id or "").strip()
        for current_server_id, server_context in context.servers.items():
            if normalized_server_id and current_server_id != normalized_server_id:
                continue
            for resource in server_context.resources:
                resources.append(
                    {
                        "server_id": resource.server_id,
                        "server_title": resource.server_title,
                        "uri": resource.uri,
                        "name": resource.name,
                        "title": resource.title,
                        "description": resource.description,
                        "mime_type": resource.mime_type,
                    }
                )
        start = max(0, int(cursor))
        stop = start + max(1, int(limit))
        page = resources[start:stop]
        return {
            "mcp_resources_count": len(page),
            "mcp_resources": page,
            "next_cursor": str(stop) if stop < len(resources) else None,
            "has_more": stop < len(resources),
        }

    async def read_resource(
        self,
        *,
        session_id: str,
        agent_id: str,
        uri: str,
        server_id: str | None,
    ) -> dict[str, Any]:
        context = self._require_context(session_id=session_id, agent_id=agent_id)
        target_context = self._select_resource_server(
            context=context,
            uri=uri,
            server_id=server_id,
        )
        if target_context.session is None:
            raise McpError(f"MCP server '{target_context.server.id}' is not connected.")
        result = await self._request_with_reconnect(
            context=context,
            server_context=target_context,
            method="resources/read",
            params={"uri": uri},
        )
        contents = result.get("contents", [])
        if not isinstance(contents, list):
            contents = []
        payload = {
            "server_id": target_context.server.id,
            "uri": uri,
            "contents": [self._sanitize_resource_content(item) for item in contents[:MCP_MAX_LIST_ITEMS]],
            "contents_truncated": len(contents) > MCP_MAX_LIST_ITEMS,
        }
        self._log_diagnostic(
            "mcp_resource_read",
            session_id=session_id,
            agent_id=agent_id,
            payload={
                "server_id": target_context.server.id,
                "uri": uri,
                "contents_count": len(payload["contents"]),
            },
        )
        return payload

    async def call_dynamic_tool(
        self,
        *,
        session_id: str,
        agent_id: str,
        synthetic_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        context = self._require_context(session_id=session_id, agent_id=agent_id)
        server_context, descriptor = self._select_dynamic_tool(
            context=context,
            synthetic_name=synthetic_name,
        )
        if server_context.session is None:
            raise McpError(f"MCP server '{server_context.server.id}' is not connected.")
        result = await self._request_with_reconnect(
            context=context,
            server_context=server_context,
            method="tools/call",
            params={
                "name": descriptor.tool_name,
                "arguments": dict(arguments),
            },
        )
        payload = self._sanitize_tool_call_result(
            result=result,
            server_context=server_context,
            descriptor=descriptor,
        )
        self._log_diagnostic(
            "mcp_tool_called",
            session_id=session_id,
            agent_id=agent_id,
            payload={
                "server_id": server_context.server.id,
                "tool_name": descriptor.tool_name,
                "synthetic_name": synthetic_name,
                "is_error": bool(payload.get("is_error", False)),
            },
        )
        return payload

    async def close_agent(self, *, session_id: str, agent_id: str) -> None:
        context = self._agent_contexts.pop((session_id, agent_id), None)
        if context is None:
            return
        session_context = self._session_contexts.get(session_id)
        if session_context is None:
            await self._close_context(context)
            return
        if any(key[0] == session_id for key in self._agent_contexts):
            return
        self._session_contexts.pop(session_id, None)
        await self._close_session_context(
            session_context,
            session_id=session_id,
            agent_id=agent_id,
        )
        self._session_locks.pop(session_id, None)

    async def close_session(self, session_id: str) -> None:
        keys = [key for key in self._agent_contexts if key[0] == session_id]
        contexts: list[_AgentMcpContext] = []
        for key in keys:
            context = self._agent_contexts.pop(key, None)
            if context is not None:
                contexts.append(context)
        session_context = self._session_contexts.pop(session_id, None)
        if session_context is not None:
            representative_agent_id = contexts[0].agent_id if contexts else "session"
            await self._close_session_context(
                session_context,
                session_id=session_id,
                agent_id=representative_agent_id,
            )
        else:
            for context in contexts:
                await self._close_context(context)
        self._session_locks.pop(session_id, None)

    async def _ensure_servers(
        self,
        *,
        context: _AgentMcpContext,
        tool_executor: "ToolExecutor",
    ) -> None:
        for server_id in context.enabled_server_ids:
            server = self.config.mcp.servers.get(server_id)
            if server is None:
                context.servers[server_id] = _AgentServerContext(
                    server=McpServerConfig(id=server_id),
                    runtime_state=McpServerRuntimeState(
                        server_id=server_id,
                        title=server_id,
                        transport="unknown",
                        enabled=True,
                        warning=f"MCP server '{server_id}' is not defined in opencompany.toml.",
                    ),
                )
                continue
            existing = context.servers.get(server_id)
            if existing is None:
                existing = _AgentServerContext(
                    server=server,
                    runtime_state=McpServerRuntimeState(
                        server_id=server_id,
                        title=server.resolved_title(),
                        transport=server.transport,
                        enabled=True,
                    ),
                )
                context.servers[server_id] = existing
            try:
                await self._ensure_server_connected(
                    context=context,
                    server_context=existing,
                    tool_executor=tool_executor,
                )
            except Exception as exc:
                self._maybe_clear_oauth_on_unauthorized(
                    server=existing.server,
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    error=exc,
                )
                await self._mark_server_unavailable(
                    context=context,
                    server_context=existing,
                    warning=self._server_warning_message(server_id=server_id, error=exc),
                )
                self._log_diagnostic(
                    "mcp_server_prepare_failed",
                    level="warning",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={
                        "server_id": server_id,
                        "transport": existing.server.transport,
                        "error": str(exc),
                    },
                )
        for server_id in list(context.servers.keys()):
            if server_id not in context.enabled_server_ids:
                removed = context.servers.pop(server_id)
                if removed.session is not None:
                    await removed.session.close()

    async def _ensure_server_connected(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
        tool_executor: "ToolExecutor",
    ) -> None:
        if server_context.session is not None:
            if server_context.runtime_state.tools_dirty:
                await self._refresh_tools(context=context, server_context=server_context)
            if server_context.runtime_state.resources_dirty:
                await self._refresh_resources(context=context, server_context=server_context)
            return
        server = server_context.server
        runtime_state = server_context.runtime_state
        roots_enabled = should_expose_roots(
            server=server,
            workspace_is_remote=context.workspace_is_remote,
        )
        runtime_state.roots_enabled = roots_enabled
        runtime_state.transport = server.transport
        if server.transport == "stdio" and context.workspace_is_remote:
            await self._mark_server_unavailable(
                context=context,
                server_context=server_context,
                warning="stdio MCP servers are disabled for remote-direct workspaces.",
            )
            return
        pending_messages: list[dict[str, Any]] = []
        pending_notifications: list[tuple[str, dict[str, Any] | None]] = []
        session_ref: dict[str, McpClientSession] = {}
        session: McpClientSession | None = None
        transport = None

        async def _on_message(message: dict[str, Any]) -> None:
            session = session_ref.get("session")
            if session is None:
                pending_messages.append(message)
                return
            await session.handle_message(message)

        async def _on_diagnostic(event_type: str, payload: dict[str, Any]) -> None:
            self._log_diagnostic(
                event_type,
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload={
                    "server_id": server.id,
                    **payload,
                },
            )

        async def _on_notification(method: str, params: dict[str, Any] | None) -> None:
            normalized_params = params if isinstance(params, dict) else {}
            if method == "notifications/tools/list_changed":
                runtime_state.tools_dirty = True
                await self._refresh_tools(context=context, server_context=server_context)
                return
            if method == "notifications/resources/list_changed":
                runtime_state.resources_dirty = True
                await self._refresh_resources(context=context, server_context=server_context)
                return
            if method == "notifications/message":
                await _on_diagnostic("mcp_server_message", normalized_params)
                return
            pending_notifications.append((method, normalized_params))

        async def _open_session(
            *,
            runtime_transport: str,
            url_override: str = "",
        ):
            nonlocal session, transport
            runtime_state.transport = runtime_transport
            runtime_url = normalize_streamable_http_url(
                str(url_override or server.url or "").strip()
            )
            transport = await self._build_transport(
                context=context,
                server=server,
                tool_executor=tool_executor,
                on_message=_on_message,
                on_diagnostic=_on_diagnostic,
                runtime_transport=runtime_transport,
                url_override=url_override,
            )
            self._log_diagnostic(
                "mcp_connect_started",
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload={
                    "server_id": server.id,
                    "transport": runtime_transport,
                    "workspace_is_remote": context.workspace_is_remote,
                    "url": runtime_url,
                },
            )
            session = McpClientSession(
                transport=transport,
                protocol_version=self.config.mcp.protocol_version,
                request_timeout_seconds=max(float(server.timeout_seconds or 30.0), 1.0),
                client_name=self.config.project.name or "OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: self._roots_for_context(
                    context=context,
                    roots_enabled=roots_enabled,
                ),
                on_notification=_on_notification,
                on_diagnostic=_on_diagnostic,
            )
            session_ref["session"] = session
            for message in pending_messages:
                await session.handle_message(message)
            return await session.initialize(roots_enabled=roots_enabled)

        try:
            try:
                initialization = await _open_session(runtime_transport=server.transport)
            except Exception as primary_exc:
                fallback_url = self._legacy_sse_fallback_url(
                    server=server,
                    error=primary_exc,
                )
                if not fallback_url:
                    raise
                self._log_diagnostic(
                    "mcp_transport_fallback_started",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={
                        "server_id": server.id,
                        "from_transport": server.transport,
                        "to_transport": "sse",
                        "fallback_url": fallback_url,
                        "error": str(primary_exc),
                    },
                )
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.close()
                elif transport is not None:
                    with contextlib.suppress(Exception):
                        await transport.close()
                session = None
                transport = None
                session_ref.pop("session", None)
                pending_messages.clear()
                pending_notifications.clear()
                try:
                    initialization = await _open_session(
                        runtime_transport="sse",
                        url_override=fallback_url,
                    )
                except Exception as fallback_exc:
                    self._log_diagnostic(
                        "mcp_transport_fallback_failed",
                        level="warning",
                        session_id=context.session_id,
                        agent_id=context.agent_id,
                        payload={
                            "server_id": server.id,
                            "from_transport": server.transport,
                            "to_transport": "sse",
                            "fallback_url": fallback_url,
                            "primary_error": str(primary_exc),
                            "fallback_error": str(fallback_exc),
                        },
                    )
                    raise fallback_exc from primary_exc
                self._log_diagnostic(
                    "mcp_transport_fallback_succeeded",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={
                        "server_id": server.id,
                        "from_transport": server.transport,
                        "to_transport": "sse",
                        "fallback_url": fallback_url,
                    },
                )
            server_context.session = session
            runtime_state.connected = True
            runtime_state.protocol_version = initialization.protocol_version
            runtime_state.warning = ""
            self._log_diagnostic(
                "mcp_initialized",
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload={
                    "server_id": server.id,
                    "server_name": initialization.server_name,
                    "server_version": initialization.server_version,
                    "protocol_version": initialization.protocol_version,
                    "roots_enabled": roots_enabled,
                },
            )
            await self._refresh_tools(context=context, server_context=server_context)
            await self._refresh_resources(context=context, server_context=server_context)
            for method, params in pending_notifications:
                await _on_notification(method, params)
        except Exception:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
            elif transport is not None:
                with contextlib.suppress(Exception):
                    await transport.close()
            raise

    async def _build_transport(
        self,
        *,
        context: _AgentMcpContext,
        server: McpServerConfig,
        tool_executor: "ToolExecutor",
        on_message: Any,
        on_diagnostic: Any,
        runtime_transport: str | None = None,
        url_override: str = "",
    ):
        normalized_transport = str(runtime_transport or server.transport or "").strip() or server.transport
        if normalized_transport == "stdio":
            transport_box: dict[str, StdioMcpTransport] = {}

            async def _on_stdout(_channel: str, text: str) -> None:
                transport = transport_box.get("transport")
                if transport is None:
                    return
                await transport.handle_stdout(text)

            async def _on_stderr(_channel: str, text: str) -> None:
                transport = transport_box.get("transport")
                if transport is None:
                    return
                await transport.handle_stderr(text)

            command = shlex.join([server.command, *server.args]) if server.args else server.command
            request = tool_executor.build_interactive_request(
                workspace_root=context.workspace_path,
                command=command,
                cwd=server.cwd or ".",
                session_id=context.session_id,
                environment=dict(server.env),
            )
            process = await tool_executor.shell_backend().start_interactive(
                request,
                on_stdout=_on_stdout,
                on_stderr=_on_stderr,
            )
            transport = StdioMcpTransport(
                process=process,
                on_message=on_message,
                on_diagnostic=on_diagnostic,
            )
            transport_box["transport"] = transport
            return transport
        headers = {
            key: expand_header_value(value)
            for key, value in server.headers.items()
            if key
        }
        oauth_provider = None
        if server.oauth_enabled:
            oauth_provider = McpOAuthTokenProvider(
                server=server,
                store_path=RuntimePaths.create(
                    self.app_dir,
                    self.config,
                ).mcp_oauth_tokens_path,
            )
        resolved_url = normalize_streamable_http_url(str(url_override or server.url or "").strip())
        user_agent = f"{(self.config.project.name or 'OpenCompany').strip() or 'OpenCompany'}/0.1.0"
        if normalized_transport == "sse":
            return LegacySseMcpTransport(
                url=resolved_url,
                headers=headers,
                timeout_seconds=max(float(server.timeout_seconds or 30.0), 1.0),
                oauth_provider=oauth_provider,
                user_agent=user_agent,
                on_message=on_message,
                on_diagnostic=on_diagnostic,
            )
        return StreamableHttpMcpTransport(
            url=resolved_url,
            headers=headers,
            protocol_version=self.config.mcp.protocol_version,
            timeout_seconds=max(float(server.timeout_seconds or 30.0), 1.0),
            oauth_provider=oauth_provider,
            user_agent=user_agent,
            on_message=on_message,
            on_diagnostic=on_diagnostic,
        )

    def _roots_for_context(
        self,
        *,
        context: _AgentMcpContext,
        roots_enabled: bool,
    ) -> list[dict[str, Any]]:
        if not roots_enabled:
            return []
        roots = [
            {
                "uri": context.workspace_path.resolve().as_uri(),
                "name": context.workspace_path.name or context.workspace_path.as_posix(),
            }
        ]
        self._log_diagnostic(
            "mcp_roots_served",
            session_id=context.session_id,
            agent_id=context.agent_id,
            payload={
                "roots_count": len(roots),
                "workspace_path": str(context.workspace_path),
            },
        )
        return roots

    async def _refresh_tools(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
    ) -> None:
        if server_context.session is None:
            return
        result = await self._list_all_pages(
            context=context,
            server_context=server_context,
            method="tools/list",
            key="tools",
        )
        raw_tools = [item for item in result if isinstance(item, dict)]
        raw_tool_names = [
            str(item.get("name", "")).strip()
            for item in raw_tools
            if str(item.get("name", "")).strip()
        ]
        allowed = set(filter_allowed_tools(raw_tool_names, server_context.server.allowed_tools))
        descriptors: list[McpToolDescriptor] = []
        for item in raw_tools:
            tool_name = str(item.get("name", "")).strip()
            if not tool_name or tool_name not in allowed:
                continue
            descriptors.append(
                McpToolDescriptor(
                    server_id=server_context.server.id,
                    server_title=server_context.server.resolved_title(),
                    tool_name=tool_name,
                    synthetic_name=synthetic_tool_name(server_context.server.id, tool_name),
                    description=str(item.get("description", "") or "").strip(),
                    title=str(item.get("title", "") or "").strip(),
                    input_schema=(
                        dict(item.get("inputSchema"))
                        if isinstance(item.get("inputSchema"), dict)
                        else {}
                    ),
                )
            )
        server_context.tool_descriptors = descriptors
        server_context.tool_by_synthetic_name = {
            descriptor.synthetic_name: descriptor for descriptor in descriptors
        }
        server_context.runtime_state.tool_count = len(descriptors)
        server_context.runtime_state.tools_dirty = False
        self._log_diagnostic(
            "mcp_tools_refreshed",
            session_id=context.session_id,
            agent_id=context.agent_id,
            payload={
                "server_id": server_context.server.id,
                "tool_count": len(descriptors),
            },
        )

    async def _refresh_resources(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
    ) -> None:
        if server_context.session is None:
            return
        try:
            result = await self._list_all_pages(
                context=context,
                server_context=server_context,
                method="resources/list",
                key="resources",
            )
        except McpRequestError as exc:
            if not self._is_method_not_found_error(exc):
                raise
            server_context.resources = []
            server_context.runtime_state.resource_count = 0
            server_context.runtime_state.resources_dirty = False
            self._log_diagnostic(
                "mcp_resources_not_supported",
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload={
                    "server_id": server_context.server.id,
                    "error": str(exc),
                },
            )
            return
        descriptors: list[McpResourceDescriptor] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri", "")).strip()
            if not uri:
                continue
            descriptors.append(
                McpResourceDescriptor(
                    server_id=server_context.server.id,
                    server_title=server_context.server.resolved_title(),
                    uri=uri,
                    name=str(item.get("name", "") or "").strip(),
                    title=str(item.get("title", "") or "").strip(),
                    description=str(item.get("description", "") or "").strip(),
                    mime_type=str(item.get("mimeType", "") or "").strip(),
                )
            )
        server_context.resources = descriptors
        server_context.runtime_state.resource_count = len(descriptors)
        server_context.runtime_state.resources_dirty = False
        self._log_diagnostic(
            "mcp_resources_refreshed",
            session_id=context.session_id,
            agent_id=context.agent_id,
            payload={
                "server_id": server_context.server.id,
                "resource_count": len(descriptors),
            },
        )

    async def _list_all_pages(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
        method: str,
        key: str,
    ) -> list[Any]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        rows: list[Any] = []
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._request_with_reconnect(
                context=context,
                server_context=server_context,
                method=method,
                params=params,
            )
            items = result.get(key, [])
            if isinstance(items, list):
                rows.extend(items[:MCP_MAX_LIST_ITEMS])
            cursor_value = result.get("nextCursor")
            cursor = str(cursor_value).strip() if cursor_value is not None else ""
            if not cursor:
                break
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            if len(rows) >= MCP_MAX_LIST_ITEMS:
                break
        return rows[:MCP_MAX_LIST_ITEMS]

    async def _close_context(self, context: _AgentMcpContext) -> None:
        for server_context in context.servers.values():
            if server_context.session is not None:
                self._log_diagnostic(
                    "mcp_shutdown",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={"server_id": server_context.server.id},
                )
                await server_context.session.close()
                server_context.session = None

    async def _close_session_context(
        self,
        session_context: _SessionMcpContext,
        *,
        session_id: str,
        agent_id: str,
    ) -> None:
        synthetic_context = _AgentMcpContext(
            session_id=session_id,
            agent_id=agent_id,
            workspace_path=session_context.workspace_path,
            workspace_is_remote=session_context.workspace_is_remote,
            enabled_server_ids=list(session_context.enabled_server_ids),
            servers=session_context.servers,
        )
        await self._close_context(synthetic_context)

    async def _request_with_reconnect(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        session = server_context.session
        if session is None:
            raise McpError(f"MCP server '{server_context.server.id}' is not connected.")
        try:
            return await session.request(method, params)
        except McpError as exc:
            if not self._is_session_not_found_error(exc):
                raise
            self._log_diagnostic(
                "mcp_reconnect_requested",
                level="warning",
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload={
                    "server_id": server_context.server.id,
                    "method": method,
                    "error": str(exc),
                },
            )
            try:
                await self._reconnect_server_context(
                    context=context,
                    server_context=server_context,
                )
            except Exception as reconnect_exc:
                await self._mark_server_unavailable(
                    context=context,
                    server_context=server_context,
                    warning=self._server_warning_message(
                        server_id=server_context.server.id,
                        error=reconnect_exc,
                    ),
                )
                self._log_diagnostic(
                    "mcp_reconnect_failed",
                    level="warning",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={
                        "server_id": server_context.server.id,
                        "method": method,
                        "error": str(reconnect_exc),
                    },
                )
                if isinstance(reconnect_exc, McpError):
                    raise
                raise McpError(
                    self._server_warning_message(
                        server_id=server_context.server.id,
                        error=reconnect_exc,
                    )
                ) from reconnect_exc
            if server_context.session is None:
                await self._mark_server_unavailable(
                    context=context,
                    server_context=server_context,
                    warning=f"MCP server '{server_context.server.id}' is not connected.",
                )
                raise
            try:
                return await server_context.session.request(method, params)
            except McpRequestError:
                raise
            except McpError as retry_exc:
                await self._mark_server_unavailable(
                    context=context,
                    server_context=server_context,
                    warning=self._server_warning_message(
                        server_id=server_context.server.id,
                        error=retry_exc,
                    ),
                )
                self._log_diagnostic(
                    "mcp_reconnect_failed",
                    level="warning",
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    payload={
                        "server_id": server_context.server.id,
                        "method": method,
                        "error": str(retry_exc),
                        "phase": "retry_request",
                    },
                )
                raise

    async def _reconnect_server_context(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
    ) -> None:
        if server_context.session is not None:
            await server_context.session.close()
            server_context.session = None
        server_context.runtime_state.connected = False
        server_context.runtime_state.tools_dirty = True
        server_context.runtime_state.resources_dirty = True
        if context.tool_executor is None:
            raise McpError(
                f"Unable to reconnect MCP server '{server_context.server.id}' without tool executor context."
            )
        await self._ensure_server_connected(
            context=context,
            server_context=server_context,
            tool_executor=context.tool_executor,
        )

    @staticmethod
    def _is_session_not_found_error(error: McpError) -> bool:
        return "404" in str(error)

    @staticmethod
    def _is_method_not_found_error(error: Exception) -> bool:
        if isinstance(error, McpRequestError):
            with contextlib.suppress(Exception):
                if int(error.code) == -32601:
                    return True
        detail = str(error or "").strip().lower()
        return "method not found" in detail or "unsupported mcp method" in detail

    def _require_context(self, *, session_id: str, agent_id: str) -> _AgentMcpContext:
        context = self._agent_contexts.get((session_id, agent_id))
        if context is None:
            raise McpError(
                f"MCP context for agent {agent_id} in session {session_id} has not been prepared."
            )
        return context

    def _select_dynamic_tool(
        self,
        *,
        context: _AgentMcpContext,
        synthetic_name: str,
    ) -> tuple[_AgentServerContext, McpToolDescriptor]:
        for server_context in context.servers.values():
            descriptor = server_context.tool_by_synthetic_name.get(synthetic_name)
            if descriptor is not None:
                return server_context, descriptor
        raise McpError(f"MCP tool '{synthetic_name}' is not available.")

    def _select_resource_server(
        self,
        *,
        context: _AgentMcpContext,
        uri: str,
        server_id: str | None,
    ) -> _AgentServerContext:
        normalized_server_id = str(server_id or "").strip()
        if normalized_server_id:
            server_context = context.servers.get(normalized_server_id)
            if server_context is None:
                raise McpError(f"MCP server '{normalized_server_id}' is not available.")
            return server_context
        matches = [
            server_context
            for server_context in context.servers.values()
            if any(resource.uri == uri for resource in server_context.resources)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise McpError(
                f"Resource URI '{uri}' exists on multiple MCP servers. Provide server_id explicitly."
            )
        raise McpError(f"Resource URI '{uri}' was not found on any enabled MCP server.")

    def _sanitize_tool_call_result(
        self,
        *,
        result: dict[str, Any],
        server_context: _AgentServerContext,
        descriptor: McpToolDescriptor,
    ) -> dict[str, Any]:
        content = result.get("content", [])
        if not isinstance(content, list):
            content = []
        structured_content = result.get("structuredContent")
        return {
            "server_id": server_context.server.id,
            "tool_name": descriptor.tool_name,
            "is_error": bool(result.get("isError", False)),
            "content": [self._sanitize_resource_content(item) for item in content[:MCP_MAX_LIST_ITEMS]],
            "content_truncated": len(content) > MCP_MAX_LIST_ITEMS,
            **(
                {"structured_content": self._sanitize_json_payload(structured_content)}
                if structured_content is not None
                else {}
            ),
        }

    async def _mark_server_unavailable(
        self,
        *,
        context: _AgentMcpContext,
        server_context: _AgentServerContext,
        warning: str,
    ) -> None:
        if server_context.session is not None:
            with contextlib.suppress(Exception):
                await server_context.session.close()
        server_context.session = None
        server_context.tool_descriptors = []
        server_context.tool_by_synthetic_name = {}
        server_context.resources = []
        server_context.runtime_state.connected = False
        server_context.runtime_state.transport = server_context.server.transport
        server_context.runtime_state.protocol_version = ""
        server_context.runtime_state.warning = str(warning or "").strip()
        server_context.runtime_state.tool_count = 0
        server_context.runtime_state.resource_count = 0
        server_context.runtime_state.tools_dirty = False
        server_context.runtime_state.resources_dirty = False

    @staticmethod
    def _server_warning_message(*, server_id: str, error: Exception) -> str:
        detail = str(error).strip()
        if detail:
            return detail
        return f"MCP server '{server_id}' is unavailable."

    @staticmethod
    def _legacy_sse_fallback_url(*, server: McpServerConfig, error: Exception) -> str:
        if server.transport != "streamable_http":
            return ""
        detail = str(error or "").strip().lower()
        if "requires oauth login" in detail or "login expired" in detail:
            return ""
        return derive_legacy_sse_url(server.url)

    def _maybe_clear_oauth_on_unauthorized(
        self,
        *,
        server: McpServerConfig,
        session_id: str,
        agent_id: str,
        error: Exception,
    ) -> None:
        if not server.oauth_enabled:
            return
        detail = str(error or "").strip().lower()
        if "401 unauthorized" not in detail:
            return
        should_clear = self._should_clear_oauth_after_unauthorized(detail)
        removed = False
        record_snapshot: dict[str, Any] = {
            "token_record_present": False,
            "resource": "",
            "authorization_server": "",
            "scope": "",
            "expires_at": None,
            "updated_at": "",
            "client_id_present": False,
            "refresh_token_present": False,
        }
        try:
            token_store = McpOAuthStore(
                RuntimePaths.create(
                    self.app_dir,
                    self.config,
                ).mcp_oauth_tokens_path
            )
            record = token_store.load_record(server.id)
            if record is not None:
                record_snapshot = {
                    "token_record_present": True,
                    "resource": str(record.resource or "").strip(),
                    "authorization_server": str(record.authorization_server or "").strip(),
                    "scope": str(record.scope or "").strip(),
                    "expires_at": record.expires_at,
                    "updated_at": str(record.updated_at or "").strip(),
                    "client_id_present": bool(str(record.client_id or "").strip()),
                    "refresh_token_present": bool(str(record.refresh_token or "").strip()),
                }
            if should_clear:
                removed = token_store.delete_record(server.id)
        except Exception as clear_exc:
            self._log_diagnostic(
                "mcp_oauth_clear_failed",
                level="warning",
                session_id=session_id,
                agent_id=agent_id,
                payload={
                    "server_id": server.id,
                    "error": str(clear_exc),
                },
            )
            return
        self._log_diagnostic(
            (
                "mcp_oauth_cleared_due_unauthorized"
                if should_clear
                else "mcp_oauth_preserved_after_unauthorized"
            ),
            level="warning",
            session_id=session_id,
            agent_id=agent_id,
            payload={
                "server_id": server.id,
                "should_clear": should_clear,
                "removed": removed,
                **record_snapshot,
            },
        )

    @staticmethod
    def _should_clear_oauth_after_unauthorized(detail: str) -> bool:
        normalized = str(detail or "").lower()
        if not normalized:
            return False
        # Preserve tokens for most 401 responses so users can inspect the
        # real server-side error and retry/re-login explicitly in UI.
        preserve_markers = (
            "restricted from accessing the public api",
            "insufficient scope",
            "insufficient_scope",
            "permission denied",
            "not authorized",
            "workspace",
        )
        if any(marker in normalized for marker in preserve_markers):
            return False
        clear_markers = (
            "requires oauth login",
            "oauth login expired",
            "refresh token is missing",
            "invalid_grant",
            "invalid_token",
            "invalid token",
        )
        return any(marker in normalized for marker in clear_markers)

    def _sanitize_json_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            text, truncated = truncate_text_payload(value)
            if truncated:
                return {"text": text, "truncated": True}
            return text
        if isinstance(value, dict):
            return {
                str(key): self._sanitize_json_payload(item)
                for key, item in list(value.items())[:MCP_MAX_LIST_ITEMS]
            }
        if isinstance(value, list):
            return [self._sanitize_json_payload(item) for item in value[:MCP_MAX_LIST_ITEMS]]
        return value

    def _sanitize_resource_content(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"value": self._sanitize_json_payload(value)}
        normalized = {
            key: self._sanitize_json_payload(item)
            for key, item in value.items()
            if key not in {"blob", "text"}
        }
        if isinstance(value.get("text"), str):
            text, truncated = truncate_text_payload(str(value.get("text", "")))
            normalized["text"] = text
            if truncated:
                normalized["truncated"] = True
        blob = value.get("blob")
        if isinstance(blob, str):
            if len(blob.encode("utf-8", errors="ignore")) > MCP_MAX_INLINE_BINARY_BYTES:
                normalized["blob_omitted"] = True
            else:
                normalized["blob"] = blob
        return normalized
