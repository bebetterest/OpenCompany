from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from opencompany.config import McpServerConfig, OpenCompanyConfig
from opencompany.mcp.manager import McpManager, _AgentMcpContext, _AgentServerContext
from opencompany.mcp.models import (
    MCP_MAX_INLINE_BINARY_BYTES,
    MCP_MAX_LIST_ITEMS,
    MCP_MAX_INLINE_TEXT_CHARS,
    McpResourceDescriptor,
    McpServerRuntimeState,
    McpToolDescriptor,
    synthetic_tool_name,
    should_expose_roots,
)
from opencompany.mcp.session import McpClientSession, McpError, McpTransport, StreamableHttpMcpTransport
from opencompany.models import AgentNode, AgentRole, RunSession


class _FakeTransport(McpTransport):
    def __init__(
        self,
        *,
        protocol_version: str = "2025-11-25",
        auto_initialize: bool = True,
    ) -> None:
        super().__init__(
            on_message=self._on_message,
            on_diagnostic=self._on_diagnostic,
        )
        self.protocol_version = protocol_version
        self.auto_initialize = auto_initialize
        self.sent: list[dict[str, Any]] = []
        self.received_messages: list[dict[str, Any]] = []
        self.received_diagnostics: list[tuple[str, dict[str, Any]]] = []
        self.started = False
        self.closed = False

    async def _on_message(self, message: dict[str, Any]) -> None:
        self.received_messages.append(message)

    async def _on_diagnostic(self, event_type: str, payload: dict[str, Any]) -> None:
        self.received_diagnostics.append((event_type, payload))

    async def start(self) -> None:
        self.started = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if self.auto_initialize and message.get("method") == "initialize" and "id" in message:
            await self.emit_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": self.protocol_version,
                        "serverInfo": {"name": "Demo MCP", "version": "1.0.0"},
                        "capabilities": {"tools": {}},
                    },
                }
            )

    async def close(self) -> None:
        self.closed = True


class _FakeHttpTransport(StreamableHttpMcpTransport):
    def __init__(self, *, server_protocol_version: str) -> None:
        super().__init__(
            url="http://127.0.0.1:8787/mcp",
            headers={},
            protocol_version="2025-11-25",
            timeout_seconds=5,
            on_message=lambda _message: None,
            on_diagnostic=lambda _event_type, _payload: None,
        )
        self.server_protocol_version = server_protocol_version
        self.sent: list[dict[str, Any]] = []
        self.reader_started = False

    async def start(self) -> None:
        return None

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if message.get("method") == "initialize" and "id" in message:
            await self.emit_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": self.server_protocol_version,
                        "serverInfo": {"name": "Demo MCP", "version": "1.0.0"},
                        "capabilities": {},
                    },
                }
            )

    async def maybe_start_reader(self) -> None:
        self.reader_started = True

    async def close(self) -> None:
        return None


class McpSessionTests(unittest.TestCase):
    def test_initialize_sends_initialized_notification(self) -> None:
        async def run() -> None:
            notifications: list[tuple[str, dict[str, Any] | None]] = []
            transport = _FakeTransport()
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [{"uri": "file:///tmp/project", "name": "project"}],
                on_notification=lambda method, params: notifications.append((method, params)),
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            result = await session.initialize(roots_enabled=True)

            self.assertTrue(transport.started)
            self.assertEqual(result.server_name, "Demo MCP")
            self.assertEqual(result.protocol_version, "2025-11-25")
            self.assertEqual(transport.sent[0]["method"], "initialize")
            self.assertIn("roots", transport.sent[0]["params"]["capabilities"])
            self.assertEqual(transport.sent[1]["method"], "notifications/initialized")
            self.assertEqual(notifications, [])

        asyncio.run(run())

    def test_handle_roots_list_request_responds_with_workspace_roots(self) -> None:
        async def run() -> None:
            transport = _FakeTransport()
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [{"uri": "file:///tmp/project", "name": "project"}],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            await session.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "roots/list",
                    "params": {},
                }
            )

            self.assertEqual(
                transport.sent,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "result": {
                            "roots": [{"uri": "file:///tmp/project", "name": "project"}],
                        },
                    }
                ],
            )

        asyncio.run(run())

    def test_handle_roots_list_request_accepts_string_id(self) -> None:
        async def run() -> None:
            transport = _FakeTransport()
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [{"uri": "file:///tmp/project", "name": "project"}],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            await session.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": "req-7",
                    "method": "roots/list",
                    "params": {},
                }
            )

            self.assertEqual(
                transport.sent,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "req-7",
                        "result": {
                            "roots": [{"uri": "file:///tmp/project", "name": "project"}],
                        },
                    }
                ],
            )

        asyncio.run(run())

    def test_initialize_rejects_unsupported_protocol_version(self) -> None:
        async def run() -> None:
            transport = _FakeTransport(protocol_version="2099-01-01")
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            with self.assertRaisesRegex(Exception, "Unsupported MCP protocol version"):
                await session.initialize(roots_enabled=False)

        asyncio.run(run())

    def test_initialize_updates_http_transport_to_negotiated_protocol_version(self) -> None:
        async def run() -> None:
            transport = _FakeHttpTransport(server_protocol_version="2025-06-18")
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            result = await session.initialize(roots_enabled=False)

            self.assertEqual(result.protocol_version, "2025-06-18")
            self.assertEqual(transport.protocol_version, "2025-06-18")
            self.assertTrue(transport.reader_started)

        asyncio.run(run())

    def test_request_times_out_when_server_never_replies(self) -> None:
        async def run() -> None:
            transport = _FakeTransport(auto_initialize=False)
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=0.01,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )

            with self.assertRaisesRegex(Exception, "timed out"):
                await session.request("tools/list", {})

        asyncio.run(run())

    def test_request_accepts_stringified_response_id(self) -> None:
        async def run() -> None:
            transport = _FakeTransport(auto_initialize=False)
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            async def _reply_with_stringified_id(message: dict[str, Any]) -> None:
                transport.sent.append(message)
                await session.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": str(message["id"]),
                        "result": {"tools": []},
                    }
                )

            transport.send = _reply_with_stringified_id  # type: ignore[method-assign]
            result = await session.request("tools/list", {})

            self.assertEqual(result, {"tools": []})

        asyncio.run(run())

    def test_request_accepts_integral_float_response_id(self) -> None:
        async def run() -> None:
            transport = _FakeTransport(auto_initialize=False)
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: [],
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            async def _reply_with_float_id(message: dict[str, Any]) -> None:
                transport.sent.append(message)
                await session.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": float(message["id"]),
                        "result": {"tools": []},
                    }
                )

            transport.send = _reply_with_float_id  # type: ignore[method-assign]
            result = await session.request("tools/list", {})

            self.assertEqual(result, {"tools": []})

        asyncio.run(run())

    def test_handle_roots_list_request_returns_internal_error_when_provider_fails(self) -> None:
        async def run() -> None:
            transport = _FakeTransport()
            session = McpClientSession(
                transport=transport,
                protocol_version="2025-11-25",
                request_timeout_seconds=5,
                client_name="OpenCompany",
                client_version="0.1.0",
                roots_provider=lambda: (_ for _ in ()).throw(RuntimeError("roots unavailable")),
                on_notification=lambda _method, _params: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._on_message = session.handle_message  # type: ignore[assignment]

            await session.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": "req-roots",
                    "method": "roots/list",
                    "params": {},
                }
            )

            self.assertEqual(
                transport.sent,
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "req-roots",
                        "error": {
                            "code": -32603,
                            "message": "roots unavailable",
                        },
                    }
                ],
            )

        asyncio.run(run())


class McpManagerTests(unittest.TestCase):
    def _manager(self, project_dir: Path) -> McpManager:
        config = OpenCompanyConfig.load(project_dir)
        diagnostics: list[tuple[str, dict[str, Any]]] = []
        return McpManager(
            app_dir=project_dir,
            config=config,
            log_diagnostic=lambda event_type, **kwargs: diagnostics.append((event_type, kwargs)),
        )

    def test_should_expose_roots_defaults_follow_transport_and_location(self) -> None:
        stdio_server = McpServerConfig(id="filesystem", transport="stdio", command="demo")
        local_http_server = McpServerConfig(
            id="docs",
            transport="streamable_http",
            url="http://127.0.0.1:8787/mcp",
        )
        remote_http_server = McpServerConfig(
            id="remote",
            transport="streamable_http",
            url="https://example.com/mcp",
        )

        self.assertTrue(should_expose_roots(server=stdio_server, workspace_is_remote=False))
        self.assertTrue(should_expose_roots(server=local_http_server, workspace_is_remote=False))
        self.assertFalse(should_expose_roots(server=remote_http_server, workspace_is_remote=False))
        self.assertFalse(should_expose_roots(server=stdio_server, workspace_is_remote=True))

    def test_prepare_agent_skips_stdio_server_for_remote_workspace(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
""".strip(),
                    encoding="utf-8",
                )
                manager = self._manager(project_dir)
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["filesystem"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )

                payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=True,
                    tool_executor=object(),
                )

                self.assertEqual(payload["enabled_server_ids"], ["filesystem"])
                self.assertEqual(len(payload["entries"]), 1)
                self.assertFalse(payload["entries"][0]["connected"])
                self.assertIn("disabled for remote-direct", payload["warnings"][0]["message"])

        asyncio.run(run())

    def test_sanitize_resource_content_truncates_large_payloads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
            manager = self._manager(project_dir)

            payload = manager._sanitize_resource_content(  # type: ignore[attr-defined]
                {
                    "text": "x" * (MCP_MAX_INLINE_TEXT_CHARS + 1),
                    "blob": "a" * (MCP_MAX_INLINE_BINARY_BYTES + 1),
                    "mimeType": "text/plain",
                }
            )

            self.assertEqual(len(payload["text"]), MCP_MAX_INLINE_TEXT_CHARS)
            self.assertTrue(payload["truncated"])
            self.assertTrue(payload["blob_omitted"])

    def test_prepare_agent_keeps_healthy_servers_when_one_server_fails(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.good]
transport = "streamable_http"
url = "http://127.0.0.1:8787/mcp"

[mcp.servers.bad]
transport = "streamable_http"
url = "http://127.0.0.1:8788/mcp"
""".strip(),
                    encoding="utf-8",
                )
                manager = self._manager(project_dir)
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["good", "bad"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )

                async def _fake_ensure_server_connected(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, tool_executor
                    if server_context.server.id == "bad":
                        raise RuntimeError("bad server is offline")
                    server_context.runtime_state.connected = True
                    server_context.runtime_state.warning = ""

                manager._ensure_server_connected = _fake_ensure_server_connected  # type: ignore[method-assign]
                payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                entries_by_id = {
                    str(item.get("id", "")): item
                    for item in payload["entries"]
                    if isinstance(item, dict)
                }
                warnings_by_id = {
                    str(item.get("server_id", "")): str(item.get("message", ""))
                    for item in payload["warnings"]
                    if isinstance(item, dict)
                }
                self.assertTrue(entries_by_id["good"]["connected"])
                self.assertFalse(entries_by_id["bad"]["connected"])
                self.assertEqual(warnings_by_id["bad"], "bad server is offline")

        asyncio.run(run())

    def test_prepare_agent_clears_stale_dynamic_state_after_server_failure(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.filesystem]
transport = "streamable_http"
url = "http://127.0.0.1:8787/mcp"
""".strip(),
                    encoding="utf-8",
                )
                manager = self._manager(project_dir)
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["filesystem"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )

                async def _seed_connected_server(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, tool_executor
                    descriptor = McpToolDescriptor(
                        server_id="filesystem",
                        server_title="filesystem",
                        tool_name="search",
                        synthetic_name=synthetic_tool_name("filesystem", "search"),
                    )
                    server_context.tool_descriptors = [descriptor]
                    server_context.tool_by_synthetic_name = {
                        descriptor.synthetic_name: descriptor,
                    }
                    server_context.resources = [
                        McpResourceDescriptor(
                            server_id="filesystem",
                            server_title="filesystem",
                            uri="file:///demo.txt",
                            name="demo.txt",
                        )
                    ]
                    server_context.runtime_state.connected = True
                    server_context.runtime_state.warning = ""
                    server_context.runtime_state.tool_count = 1
                    server_context.runtime_state.resource_count = 1

                manager._ensure_server_connected = _seed_connected_server  # type: ignore[method-assign]
                healthy_payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )
                self.assertEqual(len(healthy_payload["dynamic_tools"]), 1)
                self.assertEqual(healthy_payload["entries"][0]["tool_count"], 1)
                self.assertEqual(healthy_payload["entries"][0]["resource_count"], 1)

                async def _raise_server_failure(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, server_context, tool_executor
                    raise RuntimeError("filesystem server unavailable")

                manager._ensure_server_connected = _raise_server_failure  # type: ignore[method-assign]
                degraded_payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                self.assertEqual(degraded_payload["dynamic_tools"], [])
                self.assertFalse(degraded_payload["entries"][0]["connected"])
                self.assertEqual(degraded_payload["entries"][0]["tool_count"], 0)
                self.assertEqual(degraded_payload["entries"][0]["resource_count"], 0)
                self.assertEqual(
                    degraded_payload["warnings"][0]["message"],
                    "filesystem server unavailable",
                )

        asyncio.run(run())

    def test_sanitize_tool_call_result_marks_truncated_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
            manager = self._manager(project_dir)
            descriptor = McpToolDescriptor(
                server_id="filesystem",
                server_title="filesystem",
                tool_name="search",
                synthetic_name=synthetic_tool_name("filesystem", "search"),
            )
            server_context = type(
                "_ServerContext",
                (),
                {"server": McpServerConfig(id="filesystem")},
            )()

            payload = manager._sanitize_tool_call_result(  # type: ignore[attr-defined]
                result={
                    "content": [{"text": str(index)} for index in range(MCP_MAX_LIST_ITEMS + 1)],
                },
                server_context=server_context,
                descriptor=descriptor,
            )

            self.assertEqual(len(payload["content"]), MCP_MAX_LIST_ITEMS)
            self.assertTrue(payload["content_truncated"])

    def test_request_with_reconnect_clears_stale_state_when_reconnect_fails(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
                manager = self._manager(project_dir)
                context = _AgentMcpContext(
                    session_id="session-1",
                    agent_id="agent-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["filesystem"],
                )
                manager._agent_contexts[("session-1", "agent-1")] = context  # type: ignore[attr-defined]
                descriptor = McpToolDescriptor(
                    server_id="filesystem",
                    server_title="filesystem",
                    tool_name="search",
                    synthetic_name=synthetic_tool_name("filesystem", "search"),
                )
                server_context = _AgentServerContext(
                    server=McpServerConfig(id="filesystem", transport="streamable_http", url="http://127.0.0.1:8787/mcp"),
                    runtime_state=McpServerRuntimeState(
                        server_id="filesystem",
                        title="filesystem",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                        tool_count=1,
                        resource_count=1,
                    ),
                )
                server_context.tool_descriptors = [descriptor]
                server_context.tool_by_synthetic_name = {descriptor.synthetic_name: descriptor}
                server_context.resources = [
                    McpResourceDescriptor(
                        server_id="filesystem",
                        server_title="filesystem",
                        uri="file:///demo.txt",
                    )
                ]

                class _MissingSession:
                    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
                        del method, params
                        raise McpError("MCP HTTP session was not found (404).")

                    async def close(self) -> None:
                        return None

                server_context.session = _MissingSession()
                context.servers["filesystem"] = server_context

                async def _raise_reconnect(*, context, server_context):  # type: ignore[no-untyped-def]
                    del context, server_context
                    raise McpError("reconnect failed")

                manager._reconnect_server_context = _raise_reconnect  # type: ignore[method-assign]

                with self.assertRaisesRegex(Exception, "reconnect failed"):
                    await manager._request_with_reconnect(  # type: ignore[attr-defined]
                        context=context,
                        server_context=server_context,
                        method="tools/list",
                        params={},
                    )

                self.assertIsNone(server_context.session)
                self.assertEqual(server_context.tool_descriptors, [])
                self.assertEqual(server_context.resources, [])
                self.assertFalse(server_context.runtime_state.connected)
                self.assertEqual(server_context.runtime_state.warning, "reconnect failed")

        asyncio.run(run())
