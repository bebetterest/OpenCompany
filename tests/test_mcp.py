from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import httpx

from opencompany.config import McpServerConfig, OpenCompanyConfig
from opencompany.mcp.manager import (
    McpManager,
    _AgentMcpContext,
    _AgentServerContext,
    _SessionMcpContext,
    render_mcp_prompt,
)
from opencompany.mcp.oauth import (
    McpOAuthError,
    McpOAuthSessionRecord,
    McpOAuthStore,
    McpOAuthTokenProvider,
    complete_mcp_oauth_login,
    discover_oauth_metadata,
)
from opencompany.mcp.models import (
    MCP_MAX_INLINE_BINARY_BYTES,
    MCP_MAX_LIST_ITEMS,
    MCP_MAX_INLINE_TEXT_CHARS,
    expand_header_value,
    filter_allowed_tools,
    is_local_http_url,
    McpResourceDescriptor,
    McpServerRuntimeState,
    McpToolDescriptor,
    sanitize_identifier,
    synthetic_tool_name,
    should_expose_roots,
    truncate_text_payload,
)
from opencompany.mcp.session import (
    LegacySseMcpTransport,
    McpClientSession,
    McpError,
    McpProtocolError,
    McpRequestError,
    McpTransport,
    StdioMcpTransport,
    StreamableHttpMcpTransport,
    _SseParser,
    derive_legacy_sse_url,
    normalize_streamable_http_url,
)
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


class _FakeHttpResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text_chunks: list[str] | None = None,
        raw_content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._text_chunks = list(text_chunks or [])
        self._raw_content = raw_content
        self.request = httpx.Request("GET", "http://127.0.0.1:8787/mcp")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status={self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    async def aiter_text(self):
        for chunk in self._text_chunks:
            yield chunk

    async def aread(self) -> bytes:
        return self._raw_content


class _FakeStreamContext:
    def __init__(
        self,
        response: _FakeHttpResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> _FakeHttpResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False


class _FakeHttpClient:
    def __init__(self, streams: dict[str, list[_FakeHttpResponse | Exception]]) -> None:
        self._streams = {key: list(value) for key, value in streams.items()}
        self.calls: list[tuple[str, str, dict[str, str] | None, dict[str, Any] | None]] = []
        self.deleted: list[tuple[str, dict[str, str] | None]] = []
        self.closed = False

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeStreamContext:
        self.calls.append((method, url, headers, json))
        queue = self._streams.get(method, [])
        if not queue:
            raise AssertionError(f"Unexpected HTTP method: {method}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            return _FakeStreamContext(error=item)
        return _FakeStreamContext(item)

    async def delete(self, url: str, *, headers: dict[str, str] | None = None) -> None:
        self.deleted.append((url, headers))

    async def aclose(self) -> None:
        self.closed = True


class McpModelTests(unittest.TestCase):
    def test_model_helpers_normalize_identifiers_headers_filters_and_text(self) -> None:
        with patch.dict(os.environ, {"MCP_TOKEN": "secret"}, clear=False):
            self.assertEqual(sanitize_identifier(" 1 bad/tool ", fallback="server"), "n_1_bad_tool")
            self.assertEqual(expand_header_value("env:MCP_TOKEN"), "secret")
        self.assertEqual(expand_header_value("Bearer token"), "Bearer token")
        self.assertTrue(is_local_http_url("http://localhost:8787/mcp"))
        self.assertTrue(is_local_http_url("http://127.0.0.1:8787/mcp"))
        self.assertFalse(is_local_http_url("https://example.com/mcp"))
        self.assertEqual(
            filter_allowed_tools(["search", "read", "write"], [" read ", "write"]),
            ["read", "write"],
        )
        text, truncated = truncate_text_payload("x" * (MCP_MAX_INLINE_TEXT_CHARS + 1))
        self.assertEqual(len(text), MCP_MAX_INLINE_TEXT_CHARS)
        self.assertTrue(truncated)

    def test_tool_definition_and_prompt_rendering_normalize_schema(self) -> None:
        descriptor = McpToolDescriptor(
            server_id="filesystem",
            server_title="Filesystem",
            tool_name="search",
            synthetic_name=synthetic_tool_name("filesystem", "search"),
            description="Search files",
            input_schema={"type": "array"},
        )

        definition = descriptor.to_tool_definition()
        prompt = render_mcp_prompt(
            "zh",
            {
                "enabled_server_ids": ["filesystem"],
                "entries": [
                    {
                        "id": "filesystem",
                        "title": "Filesystem",
                        "transport": "stdio",
                        "connected": True,
                        "roots_enabled": True,
                        "tool_count": 1,
                        "resource_count": 2,
                        "warning": "offline soon",
                    }
                ],
            },
        )

        self.assertEqual(definition["function"]["parameters"]["type"], "object")
        self.assertIn("Filesystem (filesystem)", definition["function"]["description"])
        self.assertIn("已启用的 MCP Servers", prompt)
        self.assertIn("Filesystem (filesystem) [stdio]", prompt)
        self.assertIn("警告：offline soon", prompt)


class McpTransportTests(unittest.TestCase):
    def test_sse_parser_reassembles_chunked_events(self) -> None:
        parser = _SseParser()

        self.assertEqual(parser.feed('data: {"jsonrpc":"2.0"'), [])
        self.assertEqual(
            parser.feed(',"method":"ping"}\n\ndata: {"jsonrpc":"2.0","method":"pong"}\n\n'),
            [
                '{"jsonrpc":"2.0","method":"ping"}',
                '{"jsonrpc":"2.0","method":"pong"}',
            ],
        )

    def test_normalize_streamable_http_url_strips_login_query_flag(self) -> None:
        self.assertEqual(
            normalize_streamable_http_url("https://huggingface.co/mcp?login"),
            "https://huggingface.co/mcp",
        )
        self.assertEqual(
            normalize_streamable_http_url("https://example.com/mcp?login=true&mode=fast"),
            "https://example.com/mcp?mode=fast",
        )
        self.assertEqual(
            normalize_streamable_http_url("https://example.com/mcp?mode=fast"),
            "https://example.com/mcp?mode=fast",
        )

    def test_derive_legacy_sse_url_ignores_login_query_flag(self) -> None:
        self.assertEqual(
            derive_legacy_sse_url("https://huggingface.co/mcp?login"),
            "https://huggingface.co/sse",
        )

    def test_stdio_transport_parses_stdout_and_stderr(self) -> None:
        async def run() -> None:
            messages: list[dict[str, Any]] = []
            diagnostics: list[tuple[str, dict[str, Any]]] = []

            class _Process:
                async def write_line(self, text: str) -> None:
                    del text

                async def close(self) -> None:
                    return None

            transport = StdioMcpTransport(
                process=_Process(),  # type: ignore[arg-type]
                on_message=lambda message: messages.append(message),
                on_diagnostic=lambda event_type, payload: diagnostics.append((event_type, payload)),
            )

            await transport.handle_stdout('{"jsonrpc":"2.0","method":"ping"}')
            await transport.handle_stderr("stderr line")

            self.assertEqual(messages, [{"jsonrpc": "2.0", "method": "ping"}])
            self.assertEqual(
                diagnostics,
                [("mcp_stdio_stderr", {"text": "stderr line"})],
            )

        asyncio.run(run())

    def test_stdio_transport_rejects_invalid_stdout_payloads(self) -> None:
        async def run() -> None:
            class _Process:
                async def write_line(self, text: str) -> None:
                    del text

                async def close(self) -> None:
                    return None

            transport = StdioMcpTransport(
                process=_Process(),  # type: ignore[arg-type]
                on_message=lambda _message: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )

            with self.assertRaisesRegex(McpProtocolError, "Invalid MCP JSON line"):
                await transport.handle_stdout("{")
            with self.assertRaisesRegex(McpProtocolError, "must be a JSON object"):
                await transport.handle_stdout("[]")

        asyncio.run(run())

    def test_http_transport_consumes_json_list_and_sse_payloads(self) -> None:
        async def run() -> None:
            messages: list[dict[str, Any]] = []
            transport = StreamableHttpMcpTransport(
                url="http://127.0.0.1:8787/mcp",
                headers={},
                protocol_version="2025-11-25",
                timeout_seconds=5,
                on_message=lambda message: messages.append(message),
                on_diagnostic=lambda _event_type, _payload: None,
            )

            await transport._consume_http_response(  # type: ignore[arg-type]
                _FakeHttpResponse(
                    headers={"Content-Type": "application/json"},
                    raw_content=(
                        b'[{"jsonrpc":"2.0","method":"list-a"},'
                        b'{"jsonrpc":"2.0","method":"list-b"}]'
                    ),
                )
            )
            await transport._consume_http_response(  # type: ignore[arg-type]
                _FakeHttpResponse(
                    headers={"Content-Type": "text/event-stream"},
                    text_chunks=[
                        'data: {"jsonrpc":"2.0","method":"stream-a"}\n\n',
                        'data: [DONE]\n\n',
                    ],
                )
            )

            self.assertEqual(
                messages,
                [
                    {"jsonrpc": "2.0", "method": "list-a"},
                    {"jsonrpc": "2.0", "method": "list-b"},
                    {"jsonrpc": "2.0", "method": "stream-a"},
                ],
            )

        asyncio.run(run())

    def test_http_transport_reader_emits_unavailable_and_failure_diagnostics(self) -> None:
        async def run() -> None:
            diagnostics: list[tuple[str, dict[str, Any]]] = []
            transport = StreamableHttpMcpTransport(
                url="http://127.0.0.1:8787/mcp",
                headers={},
                protocol_version="2025-11-25",
                timeout_seconds=5,
                on_message=lambda _message: None,
                on_diagnostic=lambda event_type, payload: diagnostics.append((event_type, payload)),
            )
            transport._session_id = "session-http"
            transport._client = _FakeHttpClient({"GET": [_FakeHttpResponse(status_code=404)]})  # type: ignore[assignment]
            await transport._reader_loop()

            self.assertEqual(
                diagnostics,
                [("mcp_http_reader_unavailable", {"status_code": 404})],
            )

            diagnostics.clear()
            transport._client = _FakeHttpClient({"GET": [RuntimeError("boom")]})  # type: ignore[assignment]
            await transport._reader_loop()

            self.assertEqual(
                diagnostics,
                [("mcp_http_reader_failed", {"error": "boom"})],
            )

        asyncio.run(run())

    def test_http_transport_close_cancels_reader_and_closes_client(self) -> None:
        async def run() -> None:
            transport = StreamableHttpMcpTransport(
                url="http://127.0.0.1:8787/mcp",
                headers={},
                protocol_version="2025-11-25",
                timeout_seconds=5,
                on_message=lambda _message: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            client = _FakeHttpClient({"GET": []})
            transport._client = client  # type: ignore[assignment]
            transport._session_id = "session-http"
            transport._reader_task = asyncio.create_task(asyncio.sleep(30))

            await transport.close()

            self.assertIsNone(transport._reader_task)
            self.assertEqual(
                client.deleted,
                [("http://127.0.0.1:8787/mcp", await transport._request_headers())],
            )
            self.assertTrue(client.closed)

        asyncio.run(run())

    def test_http_transport_rejects_invalid_event_payloads(self) -> None:
        async def run() -> None:
            transport = StreamableHttpMcpTransport(
                url="http://127.0.0.1:8787/mcp",
                headers={},
                protocol_version="2025-11-25",
                timeout_seconds=5,
                on_message=lambda _message: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )

            with self.assertRaisesRegex(McpProtocolError, "Invalid MCP HTTP event payload"):
                await transport._handle_event_data("{")
            with self.assertRaisesRegex(McpProtocolError, "must be a JSON object"):
                await transport._handle_event_data("[]")

        asyncio.run(run())

    def test_legacy_sse_transport_discovers_message_endpoint_and_posts(self) -> None:
        async def run() -> None:
            messages: list[dict[str, Any]] = []
            transport = LegacySseMcpTransport(
                url="http://127.0.0.1:8787/sse",
                headers={},
                timeout_seconds=5,
                on_message=lambda message: messages.append(message),
                on_diagnostic=lambda _event_type, _payload: None,
            )
            client = _FakeHttpClient(
                {
                    "GET": [
                        _FakeHttpResponse(
                            headers={"Content-Type": "text/event-stream"},
                            text_chunks=[
                                "event: endpoint\ndata: /messages?session_id=demo\n\n",
                                'data: {"jsonrpc":"2.0","method":"server-ping"}\n\n',
                            ],
                        )
                    ],
                    "POST": [_FakeHttpResponse(status_code=202)],
                }
            )
            transport._client = client  # type: ignore[assignment]

            await transport.start()
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})

            self.assertEqual(
                transport.post_url,
                "http://127.0.0.1:8787/messages?session_id=demo",
            )
            self.assertEqual(messages, [{"jsonrpc": "2.0", "method": "server-ping"}])
            self.assertEqual(client.calls[0][0], "GET")
            self.assertEqual(
                client.calls[1][1],
                "http://127.0.0.1:8787/messages?session_id=demo",
            )

            await transport.close()

        asyncio.run(run())

    def test_http_transport_refreshes_oauth_token_after_401(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                store_path = Path(temp_dir) / "mcp_oauth_tokens.json"
                store = McpOAuthStore(store_path)
                store.save_record(
                    McpOAuthSessionRecord(
                        server_id="notion",
                        server_url="https://mcp.example.com/mcp",
                        resource="https://mcp.example.com/mcp",
                        resource_metadata_url=(
                            "https://mcp.example.com/mcp/.well-known/oauth-protected-resource"
                        ),
                        authorization_server="https://auth.example.com",
                        issuer="https://auth.example.com",
                        authorization_endpoint="https://auth.example.com/authorize",
                        token_endpoint="https://auth.example.com/token",
                        client_id="client-123",
                        access_token="old-access",
                        refresh_token="refresh-123",
                    )
                )

                def _oauth_handler(request: httpx.Request) -> httpx.Response:
                    self.assertEqual(str(request.url), "https://auth.example.com/token")
                    body = request.content.decode("utf-8")
                    self.assertIn("grant_type=refresh_token", body)
                    self.assertIn("resource=https%3A%2F%2Fmcp.example.com%2Fmcp", body)
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "new-access",
                            "refresh_token": "refresh-456",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                        request=request,
                    )

                provider = McpOAuthTokenProvider(
                    server=McpServerConfig(
                        id="notion",
                        transport="streamable_http",
                        url="https://mcp.example.com/mcp",
                        oauth_enabled=True,
                    ),
                    store_path=store_path,
                    http_client_factory=lambda: httpx.AsyncClient(
                        transport=httpx.MockTransport(_oauth_handler)
                    ),
                )
                transport = StreamableHttpMcpTransport(
                    url="https://mcp.example.com/mcp",
                    headers={},
                    protocol_version="2025-11-25",
                    timeout_seconds=5,
                    oauth_provider=provider,
                    on_message=lambda _message: None,
                    on_diagnostic=lambda _event_type, _payload: None,
                )
                transport._client = _FakeHttpClient(
                    {
                        "POST": [
                            _FakeHttpResponse(
                                status_code=401,
                                headers={"WWW-Authenticate": "Bearer realm=\"demo\""},
                            ),
                            _FakeHttpResponse(
                                headers={"Content-Type": "application/json"},
                                raw_content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
                            ),
                        ]
                    }
                )  # type: ignore[assignment]

                await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})

                first_call = transport._client.calls[0]
                second_call = transport._client.calls[1]
                self.assertEqual(first_call[2]["Authorization"], "Bearer old-access")
                self.assertEqual(second_call[2]["Authorization"], "Bearer new-access")
                stored = store.load_record("notion")
                assert stored is not None
                self.assertEqual(stored.refresh_token, "refresh-456")

        asyncio.run(run())

    def test_oauth_provider_normalizes_lowercase_bearer_scheme(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                store_path = Path(temp_dir) / "mcp_oauth_tokens.json"
                store = McpOAuthStore(store_path)
                store.save_record(
                    McpOAuthSessionRecord(
                        server_id="notion",
                        server_url="https://mcp.example.com/mcp",
                        resource="https://mcp.example.com",
                        resource_metadata_url=(
                            "https://mcp.example.com/.well-known/oauth-protected-resource"
                        ),
                        authorization_server="https://auth.example.com",
                        issuer="https://auth.example.com",
                        authorization_endpoint="https://auth.example.com/authorize",
                        token_endpoint="https://auth.example.com/token",
                        client_id="client-123",
                        access_token="access-xyz",
                        refresh_token="refresh-xyz",
                        token_type="bearer",
                    )
                )
                provider = McpOAuthTokenProvider(
                    server=McpServerConfig(
                        id="notion",
                        transport="streamable_http",
                        url="https://mcp.example.com/mcp",
                        oauth_enabled=True,
                    ),
                    store_path=store_path,
                )

                header = await provider.authorization_header()
                self.assertEqual(header, "Bearer access-xyz")

        asyncio.run(run())

    def test_http_transport_unauthorized_error_includes_response_details(self) -> None:
        async def run() -> None:
            transport = StreamableHttpMcpTransport(
                url="https://mcp.notion.com/mcp",
                headers={},
                protocol_version="2025-11-25",
                timeout_seconds=5,
                on_message=lambda _message: None,
                on_diagnostic=lambda _event_type, _payload: None,
            )
            transport._client = _FakeHttpClient(
                {
                    "POST": [
                        _FakeHttpResponse(
                            status_code=401,
                            headers={
                                "Content-Type": "application/json",
                                "WWW-Authenticate": 'Bearer realm="OAuth", error="invalid_token"',
                            },
                            raw_content=(
                                b'{"error":"invalid_token","error_description":"Invalid token format"}'
                            ),
                        ),
                    ]
                }
            )  # type: ignore[assignment]

            with self.assertRaisesRegex(McpError, "error_description=Invalid token format"):
                await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})

        asyncio.run(run())

    def test_oauth_refresh_coalesces_concurrent_token_rotation(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                store_path = Path(temp_dir) / "mcp_oauth_tokens.json"
                store = McpOAuthStore(store_path)
                store.save_record(
                    McpOAuthSessionRecord(
                        server_id="notion",
                        server_url="https://mcp.example.com/mcp",
                        resource="https://mcp.example.com/mcp",
                        resource_metadata_url=(
                            "https://mcp.example.com/mcp/.well-known/oauth-protected-resource"
                        ),
                        authorization_server="https://auth.example.com",
                        issuer="https://auth.example.com",
                        authorization_endpoint="https://auth.example.com/authorize",
                        token_endpoint="https://auth.example.com/token",
                        client_id="client-123",
                        access_token="old-access",
                        refresh_token="refresh-123",
                    )
                )
                refresh_calls = 0

                def _oauth_handler(request: httpx.Request) -> httpx.Response:
                    nonlocal refresh_calls
                    refresh_calls += 1
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "new-access",
                            "refresh_token": "refresh-456",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                        request=request,
                    )

                def _http_client_factory() -> httpx.AsyncClient:
                    return httpx.AsyncClient(transport=httpx.MockTransport(_oauth_handler))

                provider_a = McpOAuthTokenProvider(
                    server=McpServerConfig(
                        id="notion",
                        transport="streamable_http",
                        url="https://mcp.example.com/mcp",
                        oauth_enabled=True,
                    ),
                    store_path=store_path,
                    http_client_factory=_http_client_factory,
                )
                provider_b = McpOAuthTokenProvider(
                    server=McpServerConfig(
                        id="notion",
                        transport="streamable_http",
                        url="https://mcp.example.com/mcp",
                        oauth_enabled=True,
                    ),
                    store_path=store_path,
                    http_client_factory=_http_client_factory,
                )

                refreshed = await asyncio.gather(
                    provider_a.refresh_on_unauthorized(
                        failed_authorization="Bearer old-access"
                    ),
                    provider_b.refresh_on_unauthorized(
                        failed_authorization="Bearer old-access"
                    ),
                )

                self.assertEqual(refreshed, [True, True])
                self.assertEqual(refresh_calls, 1)
                stored = store.load_record("notion")
                assert stored is not None
                self.assertEqual(stored.access_token, "new-access")
                self.assertEqual(stored.refresh_token, "refresh-456")

        asyncio.run(run())

    def test_discover_oauth_metadata_prefers_protected_resource_value(self) -> None:
        async def run() -> None:
            def _oauth_handler(request: httpx.Request) -> httpx.Response:
                url = str(request.url)
                if url == "https://mcp.notion.com/mcp/.well-known/oauth-protected-resource":
                    return httpx.Response(
                        401,
                        json={
                            "error": "invalid_token",
                            "error_description": "Missing or invalid access token",
                        },
                        request=request,
                    )
                if url == "https://mcp.notion.com/.well-known/oauth-protected-resource":
                    return httpx.Response(
                        200,
                        json={
                            "resource": "https://mcp.notion.com",
                            "authorization_servers": ["https://mcp.notion.com"],
                        },
                        request=request,
                    )
                if url == "https://mcp.notion.com/.well-known/oauth-authorization-server":
                    return httpx.Response(
                        200,
                        json={
                            "issuer": "https://mcp.notion.com",
                            "authorization_endpoint": "https://mcp.notion.com/authorize",
                            "token_endpoint": "https://mcp.notion.com/token",
                            "registration_endpoint": "https://mcp.notion.com/register",
                            "code_challenge_methods_supported": ["S256"],
                        },
                        request=request,
                    )
                raise AssertionError(f"Unexpected OAuth URL: {url}")

            metadata = await discover_oauth_metadata(
                "https://mcp.notion.com/mcp",
                http_client_factory=lambda: httpx.AsyncClient(
                    transport=httpx.MockTransport(_oauth_handler)
                ),
            )

            self.assertEqual(metadata.resource, "https://mcp.notion.com")
            self.assertEqual(
                metadata.resource_metadata_url,
                "https://mcp.notion.com/.well-known/oauth-protected-resource",
            )
            self.assertEqual(metadata.authorization_server, "https://mcp.notion.com")

        asyncio.run(run())

    def test_discover_oauth_metadata_skips_non_json_protected_resource_candidate(self) -> None:
        async def run() -> None:
            def _oauth_handler(request: httpx.Request) -> httpx.Response:
                url = str(request.url)
                if url == "https://huggingface.co/mcp/.well-known/oauth-protected-resource":
                    return httpx.Response(
                        200,
                        text="<html><body>HF MCP Server</body></html>",
                        headers={"content-type": "text/html; charset=utf-8"},
                        request=request,
                    )
                if url == "https://huggingface.co/.well-known/oauth-protected-resource":
                    return httpx.Response(
                        200,
                        json={
                            "resource": "https://huggingface.co/mcp",
                            "authorization_servers": ["https://huggingface.co"],
                        },
                        request=request,
                    )
                if url == "https://huggingface.co/.well-known/oauth-authorization-server":
                    return httpx.Response(
                        200,
                        json={
                            "issuer": "https://huggingface.co",
                            "authorization_endpoint": "https://huggingface.co/oauth/authorize",
                            "token_endpoint": "https://huggingface.co/oauth/token",
                        },
                        request=request,
                    )
                raise AssertionError(f"Unexpected OAuth URL: {url}")

            metadata = await discover_oauth_metadata(
                "https://huggingface.co/mcp",
                http_client_factory=lambda: httpx.AsyncClient(
                    transport=httpx.MockTransport(_oauth_handler)
                ),
            )

            self.assertEqual(metadata.resource, "https://huggingface.co/mcp")
            self.assertEqual(
                metadata.resource_metadata_url,
                "https://huggingface.co/.well-known/oauth-protected-resource",
            )
            self.assertEqual(metadata.authorization_server, "https://huggingface.co")
            self.assertEqual(
                metadata.authorization_endpoint,
                "https://huggingface.co/oauth/authorize",
            )
            self.assertEqual(
                metadata.token_endpoint,
                "https://huggingface.co/oauth/token",
            )

        asyncio.run(run())

    def test_complete_mcp_oauth_login_reports_timeout_with_actionable_error(self) -> None:
        async def run() -> None:
            seen: dict[str, str] = {}

            def _oauth_handler(request: httpx.Request) -> httpx.Response:
                url = str(request.url)
                if url == "https://mcp.example.com/mcp/.well-known/oauth-protected-resource":
                    return httpx.Response(
                        200,
                        json={"authorization_servers": ["https://auth.example.com"]},
                        request=request,
                    )
                if url == "https://auth.example.com/.well-known/oauth-authorization-server":
                    return httpx.Response(
                        200,
                        json={
                            "issuer": "https://auth.example.com",
                            "authorization_endpoint": "https://auth.example.com/authorize",
                            "token_endpoint": "https://auth.example.com/token",
                        },
                        request=request,
                    )
                raise AssertionError(f"Unexpected OAuth URL: {url}")

            class _TimeoutCallbackServer:
                def __init__(self) -> None:
                    self.redirect_uri = "http://127.0.0.1/fake-callback"

                async def __aenter__(self) -> "_TimeoutCallbackServer":
                    return self

                async def __aexit__(self, exc_type, exc, tb) -> None:
                    del exc_type, exc, tb

                async def wait_for_callback(self, *, timeout_seconds: float) -> dict[str, str]:
                    del timeout_seconds
                    raise TimeoutError()

            with self.assertRaisesRegex(
                McpOAuthError,
                "OAuth login timed out for server 'docs' after 8s",
            ):
                await complete_mcp_oauth_login(
                    server=McpServerConfig(
                        id="docs",
                        transport="streamable_http",
                        url="https://mcp.example.com/mcp",
                        oauth_enabled=True,
                        oauth_client_id="client-123",
                    ),
                    store_path=Path("/tmp/unused-mcp-oauth-timeout.json"),
                    timeout_seconds=8.0,
                    open_browser=False,
                    authorization_url_callback=lambda url: seen.__setitem__(
                        "authorization_url", url
                    ),
                    http_client_factory=lambda: httpx.AsyncClient(
                        transport=httpx.MockTransport(_oauth_handler)
                    ),
                    callback_server_factory=_TimeoutCallbackServer,
                )

            self.assertTrue(
                str(seen.get("authorization_url", "")).startswith(
                    "https://auth.example.com/authorize?"
                )
            )

        asyncio.run(run())

    def test_complete_mcp_oauth_login_registers_client_and_persists_tokens(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                store_path = Path(temp_dir) / "mcp_oauth_tokens.json"
                seen: dict[str, str] = {}

                def _oauth_handler(request: httpx.Request) -> httpx.Response:
                    url = str(request.url)
                    if url == "https://mcp.notion.com/mcp/.well-known/oauth-protected-resource":
                        return httpx.Response(
                            200,
                            json={"authorization_servers": ["https://auth.notion.com"]},
                            request=request,
                        )
                    if url == "https://auth.notion.com/.well-known/oauth-authorization-server":
                        return httpx.Response(
                            200,
                            json={
                                "issuer": "https://auth.notion.com",
                                "authorization_endpoint": "https://auth.notion.com/authorize",
                                "token_endpoint": "https://auth.notion.com/token",
                                "registration_endpoint": "https://auth.notion.com/register",
                                "code_challenge_methods_supported": ["S256"],
                            },
                            request=request,
                        )
                    if url == "https://auth.notion.com/register":
                        seen["registration_body"] = request.content.decode("utf-8")
                        return httpx.Response(
                            200,
                            json={"client_id": "client-xyz"},
                            request=request,
                        )
                    if url == "https://auth.notion.com/token":
                        seen["token_body"] = request.content.decode("utf-8")
                        seen["token_user_agent"] = request.headers.get("User-Agent", "")
                        return httpx.Response(
                            200,
                            json={
                                "access_token": "access-xyz",
                                "refresh_token": "refresh-xyz",
                                "token_type": "Bearer",
                                "expires_in": 3600,
                            },
                            request=request,
                        )
                    raise AssertionError(f"Unexpected OAuth URL: {url}")

                def _browser_opener(url: str) -> bool:
                    parsed = httpx.URL(url)
                    seen["redirect_uri"] = parsed.params["redirect_uri"]
                    seen["state"] = parsed.params["state"]
                    seen["authorization_url"] = url
                    return True

                class _FakeCallbackServer:
                    def __init__(self) -> None:
                        self.redirect_uri = "http://127.0.0.1/fake-callback"

                    async def __aenter__(self) -> "_FakeCallbackServer":
                        return self

                    async def __aexit__(self, exc_type, exc, tb) -> None:
                        del exc_type, exc, tb

                    async def wait_for_callback(self, *, timeout_seconds: float) -> dict[str, str]:
                        del timeout_seconds
                        return {
                            "code": "code-xyz",
                            "state": seen["state"],
                        }

                result = await complete_mcp_oauth_login(
                    server=McpServerConfig(
                        id="notion",
                        transport="streamable_http",
                        url="https://mcp.notion.com/mcp",
                        oauth_enabled=True,
                        oauth_client_name="OpenCompany Test Client",
                        oauth_authorization_prompt="consent",
                        oauth_use_resource_param=False,
                    ),
                    store_path=store_path,
                    timeout_seconds=10.0,
                    open_browser=True,
                    browser_opener=_browser_opener,
                    http_client_factory=lambda: httpx.AsyncClient(
                        transport=httpx.MockTransport(_oauth_handler)
                    ),
                    callback_server_factory=_FakeCallbackServer,
                )

                self.assertTrue(result.browser_opened)
                self.assertIn("prompt=consent", seen["authorization_url"])
                self.assertNotIn("resource=", seen["authorization_url"])
                registration_payload = json.loads(seen["registration_body"])
                self.assertEqual(
                    registration_payload["token_endpoint_auth_method"],
                    "none",
                )
                self.assertEqual(
                    registration_payload["redirect_uris"],
                    ["http://127.0.0.1/fake-callback"],
                )
                self.assertNotIn("resource=", seen["token_body"])
                self.assertEqual(seen["token_user_agent"], "OpenCompany/0.1.0")
                stored = McpOAuthStore(store_path).load_record("notion")
                assert stored is not None
                self.assertEqual(stored.client_id, "client-xyz")
                self.assertEqual(stored.access_token, "access-xyz")
                self.assertEqual(stored.refresh_token, "refresh-xyz")

        asyncio.run(run())


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

    def test_notify_ping_and_unsupported_request_paths_are_predictable(self) -> None:
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

            await session.notify("notifications/ping", {"ok": True})
            await session.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "ping",
                    "params": {},
                }
            )
            await session.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "unsupported/method",
                    "params": {},
                }
            )

            self.assertEqual(transport.sent[0]["method"], "notifications/ping")
            self.assertEqual(transport.sent[1], {"jsonrpc": "2.0", "id": 9, "result": {}})
            self.assertEqual(
                transport.sent[2],
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "error": {
                        "code": -32601,
                        "message": "Unsupported MCP method 'unsupported/method'.",
                    },
                },
            )

        asyncio.run(run())

    def test_request_surfaces_server_errors_as_mcp_request_error(self) -> None:
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

            async def _reply_with_error(message: dict[str, Any]) -> None:
                transport.sent.append(message)
                await session.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32001,
                            "message": "bad request",
                            "data": {"detail": "oops"},
                        },
                    }
                )

            transport.send = _reply_with_error  # type: ignore[method-assign]

            with self.assertRaises(McpRequestError) as ctx:
                await session.request("tools/list", {})

            self.assertEqual(ctx.exception.code, -32001)
            self.assertEqual(ctx.exception.data, {"detail": "oops"})

        asyncio.run(run())

    def test_close_rejects_pending_requests(self) -> None:
        async def run() -> None:
            release_send = asyncio.Event()
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

            async def _blocking_send(message: dict[str, Any]) -> None:
                transport.sent.append(message)
                await release_send.wait()

            transport.send = _blocking_send  # type: ignore[method-assign]
            pending = asyncio.create_task(session.request("tools/list", {}))
            await asyncio.sleep(0)
            release_send.set()
            await session.close()

            with self.assertRaisesRegex(McpError, "session closed"):
                await pending

        asyncio.run(run())

    def test_pending_key_normalizes_supported_response_ids(self) -> None:
        self.assertEqual(McpClientSession._pending_key(1), "1")
        self.assertEqual(McpClientSession._pending_key(1.0), "1")
        self.assertEqual(McpClientSession._pending_key(" req-1 "), "req-1")
        self.assertIsNone(McpClientSession._pending_key(True))


class McpManagerTests(unittest.TestCase):
    def _manager(self, project_dir: Path) -> McpManager:
        manager, _diagnostics = self._manager_with_diagnostics(project_dir)
        return manager

    def _manager_with_diagnostics(
        self,
        project_dir: Path,
    ) -> tuple[McpManager, list[tuple[str, dict[str, Any]]]]:
        config = OpenCompanyConfig.load(project_dir)
        diagnostics: list[tuple[str, dict[str, Any]]] = []
        return (
            McpManager(
                app_dir=project_dir,
                config=config,
                log_diagnostic=lambda event_type, **kwargs: diagnostics.append((event_type, kwargs)),
            ),
            diagnostics,
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

    def test_available_servers_normalization_and_session_state_cover_configured_and_missing_servers(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text(
                """
[mcp.servers.filesystem]
transport = "stdio"
command = "demo"

[mcp.servers.docs]
transport = "streamable_http"
url = "http://127.0.0.1:8787/mcp"
enabled = false
title = "Docs"
""".strip(),
                encoding="utf-8",
            )
            manager = self._manager(project_dir)

            available = manager.available_servers()
            default_enabled = manager.normalize_enabled_server_ids(None)
            explicit_enabled = manager.normalize_enabled_server_ids(["docs", "filesystem", "docs"])
            session_state = manager.session_state(
                enabled_server_ids=["filesystem", "missing-server"],
            )

            self.assertEqual([item["id"] for item in available], ["docs", "filesystem"])
            self.assertEqual(default_enabled, ["filesystem"])
            self.assertEqual(explicit_enabled, ["docs", "filesystem"])
            self.assertEqual(
                [item["id"] for item in session_state["entries"]],
                ["filesystem"],
            )
            self.assertEqual(
                session_state["warnings"][0]["message"],
                "MCP server 'missing-server' is not defined in opencompany.toml.",
            )

    def test_build_transport_normalizes_huggingface_login_url(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.huggingface]
transport = "streamable_http"
url = "https://huggingface.co/mcp?login"
oauth_enabled = true
""".strip(),
                    encoding="utf-8",
                )
                manager = self._manager(project_dir)
                server = manager.config.mcp.servers["huggingface"]
                context = _AgentMcpContext(
                    session_id="session-1",
                    agent_id="agent-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["huggingface"],
                )

                transport = await manager._build_transport(  # type: ignore[attr-defined]
                    context=context,
                    server=server,
                    tool_executor=object(),
                    on_message=lambda _message: None,
                    on_diagnostic=lambda _event_type, _payload: None,
                    runtime_transport="streamable_http",
                    url_override="",
                )

                self.assertIsInstance(transport, StreamableHttpMcpTransport)
                self.assertEqual(transport.url, "https://huggingface.co/mcp")
                await transport.close()

        asyncio.run(run())

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

    def test_prepare_agent_reuses_session_mcp_context_across_agents(self) -> None:
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
                root_agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )
                worker_agent = AgentNode(
                    id="agent-worker",
                    session_id="session-1",
                    name="Worker",
                    role=AgentRole.WORKER,
                    instruction="demo",
                    workspace_id="workspace-worker",
                )

                expensive_initializations = 0

                async def _fake_ensure_server_connected(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, tool_executor
                    nonlocal expensive_initializations
                    if server_context.session is None:
                        expensive_initializations += 1
                        server_context.session = object()  # type: ignore[assignment]
                    server_context.runtime_state.connected = True
                    server_context.runtime_state.warning = ""

                manager._ensure_server_connected = _fake_ensure_server_connected  # type: ignore[method-assign]

                first_payload = await manager.prepare_agent(
                    session=session,
                    agent=root_agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )
                second_payload = await manager.prepare_agent(
                    session=session,
                    agent=worker_agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                self.assertEqual(expensive_initializations, 1)
                self.assertTrue(first_payload["entries"][0]["connected"])
                self.assertTrue(second_payload["entries"][0]["connected"])
                root_context = manager._agent_contexts[("session-1", "agent-root")]  # type: ignore[attr-defined]
                worker_context = manager._agent_contexts[("session-1", "agent-worker")]  # type: ignore[attr-defined]
                self.assertIs(root_context.servers, worker_context.servers)

        asyncio.run(run())

    def test_refresh_tools_and_resources_apply_filters_and_clear_dirty_flags(self) -> None:
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
                server_context = _AgentServerContext(
                    server=McpServerConfig(
                        id="filesystem",
                        title="Filesystem",
                        transport="streamable_http",
                        url="http://127.0.0.1:8787/mcp",
                        allowed_tools=["read_file"],
                    ),
                    runtime_state=McpServerRuntimeState(
                        server_id="filesystem",
                        title="Filesystem",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                        tools_dirty=True,
                        resources_dirty=True,
                    ),
                )

                class _Session:
                    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
                        del params
                        if method == "tools/list":
                            return {
                                "tools": [
                                    {
                                        "name": "read_file",
                                        "description": "read",
                                        "inputSchema": {"type": "object"},
                                    },
                                    {
                                        "name": "write_file",
                                        "description": "write",
                                    },
                                ]
                            }
                        if method == "resources/list":
                            return {
                                "resources": [
                                    {"uri": "file:///demo.txt", "name": "demo.txt"},
                                    {"name": "missing-uri"},
                                ]
                            }
                        raise AssertionError(method)

                server_context.session = _Session()  # type: ignore[assignment]

                await manager._refresh_tools(context=context, server_context=server_context)  # type: ignore[attr-defined]
                await manager._refresh_resources(context=context, server_context=server_context)  # type: ignore[attr-defined]

                self.assertEqual(
                    [item.tool_name for item in server_context.tool_descriptors],
                    ["read_file"],
                )
                self.assertEqual(
                    list(server_context.tool_by_synthetic_name),
                    [synthetic_tool_name("filesystem", "read_file")],
                )
                self.assertEqual(
                    [item.uri for item in server_context.resources],
                    ["file:///demo.txt"],
                )
                self.assertFalse(server_context.runtime_state.tools_dirty)
                self.assertFalse(server_context.runtime_state.resources_dirty)

        asyncio.run(run())

    def test_refresh_resources_treats_method_not_found_as_optional_capability(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
                manager, diagnostics = self._manager_with_diagnostics(project_dir)
                context = _AgentMcpContext(
                    session_id="session-1",
                    agent_id="agent-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["huggingface"],
                )
                server_context = _AgentServerContext(
                    server=McpServerConfig(
                        id="huggingface",
                        title="Hugging Face MCP",
                        transport="streamable_http",
                        url="https://huggingface.co/mcp",
                    ),
                    runtime_state=McpServerRuntimeState(
                        server_id="huggingface",
                        title="Hugging Face MCP",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                        resources_dirty=True,
                    ),
                )

                class _Session:
                    async def request(
                        self,
                        method: str,
                        params: dict[str, Any] | None,
                    ) -> dict[str, Any]:
                        del params
                        if method == "resources/list":
                            raise McpRequestError(code=-32601, message="Method not found")
                        raise AssertionError(method)

                server_context.session = _Session()  # type: ignore[assignment]

                await manager._refresh_resources(  # type: ignore[attr-defined]
                    context=context,
                    server_context=server_context,
                )

                self.assertEqual(server_context.resources, [])
                self.assertEqual(server_context.runtime_state.resource_count, 0)
                self.assertFalse(server_context.runtime_state.resources_dirty)
                self.assertTrue(server_context.runtime_state.connected)
                self.assertEqual(server_context.runtime_state.warning, "")
                self.assertIn(
                    "mcp_resources_not_supported",
                    [event_type for event_type, _payload in diagnostics],
                )

        asyncio.run(run())

    def test_list_all_pages_stops_on_repeated_cursor_and_max_limit(self) -> None:
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
                server_context = _AgentServerContext(
                    server=McpServerConfig(id="filesystem", transport="streamable_http", url="http://127.0.0.1:8787/mcp"),
                    runtime_state=McpServerRuntimeState(
                        server_id="filesystem",
                        title="filesystem",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                    ),
                )
                replies = [
                    {"tools": [{"name": "tool-1"}], "nextCursor": "cursor-1"},
                    {"tools": [{"name": "tool-2"}], "nextCursor": "cursor-1"},
                ]

                async def _fake_request_with_reconnect(*, context, server_context, method, params):  # type: ignore[no-untyped-def]
                    del context, server_context, method, params
                    return replies.pop(0)

                manager._request_with_reconnect = _fake_request_with_reconnect  # type: ignore[method-assign]

                rows = await manager._list_all_pages(  # type: ignore[attr-defined]
                    context=context,
                    server_context=server_context,
                    method="tools/list",
                    key="tools",
                )

                self.assertEqual(rows, [{"name": "tool-1"}, {"name": "tool-2"}])

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

    def test_select_resource_server_rejects_ambiguous_uri(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
            manager = self._manager(project_dir)
            context = _AgentMcpContext(
                session_id="session-1",
                agent_id="agent-1",
                workspace_path=project_dir,
                workspace_is_remote=False,
                enabled_server_ids=["filesystem", "docs"],
                servers={
                    "filesystem": _AgentServerContext(
                        server=McpServerConfig(id="filesystem"),
                        runtime_state=McpServerRuntimeState(
                            server_id="filesystem",
                            title="filesystem",
                            transport="stdio",
                            enabled=True,
                        ),
                        resources=[
                            McpResourceDescriptor(
                                server_id="filesystem",
                                server_title="filesystem",
                                uri="file:///demo.txt",
                            )
                        ],
                    ),
                    "docs": _AgentServerContext(
                        server=McpServerConfig(id="docs"),
                        runtime_state=McpServerRuntimeState(
                            server_id="docs",
                            title="docs",
                            transport="streamable_http",
                            enabled=True,
                        ),
                        resources=[
                            McpResourceDescriptor(
                                server_id="docs",
                                server_title="docs",
                                uri="file:///demo.txt",
                            )
                        ],
                    ),
                },
            )

            with self.assertRaisesRegex(McpError, "exists on multiple MCP servers"):
                manager._select_resource_server(  # type: ignore[attr-defined]
                    context=context,
                    uri="file:///demo.txt",
                    server_id=None,
                )

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

    def test_prepare_agent_preserves_oauth_record_after_generic_unauthorized_failure(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.notion]
transport = "streamable_http"
url = "https://mcp.notion.com/mcp"
oauth_enabled = true
""".strip(),
                    encoding="utf-8",
                )
                manager, diagnostics = self._manager_with_diagnostics(project_dir)
                store = McpOAuthStore(project_dir / ".opencompany" / "mcp_oauth_tokens.json")
                store.save_record(
                    McpOAuthSessionRecord(
                        server_id="notion",
                        server_url="https://mcp.notion.com/mcp",
                        resource="https://mcp.notion.com/mcp",
                        resource_metadata_url=(
                            "https://mcp.notion.com/mcp/.well-known/oauth-protected-resource"
                        ),
                        authorization_server="https://mcp.notion.com",
                        issuer="https://mcp.notion.com",
                        authorization_endpoint="https://mcp.notion.com/authorize",
                        token_endpoint="https://mcp.notion.com/token",
                        client_id="client-1",
                        access_token="access-1",
                        refresh_token="refresh-1",
                    )
                )
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["notion"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )

                async def _raise_unauthorized(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, server_context, tool_executor
                    raise RuntimeError(
                        "Client error '401 Unauthorized' for url 'https://mcp.notion.com/mcp'"
                    )

                manager._ensure_server_connected = _raise_unauthorized  # type: ignore[method-assign]
                payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                self.assertFalse(payload["entries"][0]["connected"])
                self.assertIsNotNone(store.load_record("notion"))
                self.assertIn(
                    "mcp_oauth_preserved_after_unauthorized",
                    [event for event, _payload in diagnostics],
                )

        asyncio.run(run())

    def test_prepare_agent_clears_oauth_record_after_invalid_token_unauthorized_failure(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.huggingface]
transport = "streamable_http"
url = "https://huggingface.co/mcp?login"
oauth_enabled = true
""".strip(),
                    encoding="utf-8",
                )
                manager, diagnostics = self._manager_with_diagnostics(project_dir)
                store = McpOAuthStore(project_dir / ".opencompany" / "mcp_oauth_tokens.json")
                store.save_record(
                    McpOAuthSessionRecord(
                        server_id="huggingface",
                        server_url="https://huggingface.co/mcp",
                        resource="https://huggingface.co/mcp",
                        resource_metadata_url=(
                            "https://huggingface.co/.well-known/oauth-protected-resource"
                        ),
                        authorization_server="https://huggingface.co",
                        issuer="https://huggingface.co",
                        authorization_endpoint="https://huggingface.co/oauth/authorize",
                        token_endpoint="https://huggingface.co/oauth/token",
                        client_id="client-1",
                        access_token="access-1",
                        refresh_token="refresh-1",
                    )
                )
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["huggingface"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )

                async def _raise_invalid_token(*, context, server_context, tool_executor) -> None:  # type: ignore[no-untyped-def]
                    del context, server_context, tool_executor
                    raise RuntimeError(
                        "Client error '401 Unauthorized' for url 'https://huggingface.co/mcp'. "
                        "response=error=invalid_token, error_description=Invalid token format"
                    )

                manager._ensure_server_connected = _raise_invalid_token  # type: ignore[method-assign]
                payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                self.assertFalse(payload["entries"][0]["connected"])
                self.assertIsNone(store.load_record("huggingface"))
                self.assertIn(
                    "mcp_oauth_cleared_due_unauthorized",
                    [event for event, _payload in diagnostics],
                )

        asyncio.run(run())

    def test_prepare_agent_falls_back_to_sse_when_streamable_http_init_fails(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text(
                    """
[mcp.servers.notion]
transport = "streamable_http"
url = "https://mcp.notion.com/mcp"
""".strip(),
                    encoding="utf-8",
                )
                manager, diagnostics = self._manager_with_diagnostics(project_dir)
                session = RunSession(
                    id="session-1",
                    project_dir=project_dir,
                    task="demo",
                    locale="en",
                    root_agent_id="agent-root",
                    enabled_mcp_server_ids=["notion"],
                )
                agent = AgentNode(
                    id="agent-root",
                    session_id="session-1",
                    name="Root",
                    role=AgentRole.ROOT,
                    instruction="demo",
                    workspace_id="workspace-root",
                )
                attempts: list[tuple[str | None, str]] = []

                async def _noop_refresh(*, context, server_context) -> None:  # type: ignore[no-untyped-def]
                    del context, server_context

                class _FailingTransport(McpTransport):
                    def __init__(self, *, on_message, on_diagnostic) -> None:  # type: ignore[no-untyped-def]
                        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)

                    async def start(self) -> None:
                        return None

                    async def send(self, message: dict[str, Any]) -> None:
                        del message
                        request = httpx.Request("POST", "https://mcp.notion.com/mcp")
                        raise httpx.HTTPStatusError(
                            "401 Unauthorized",
                            request=request,
                            response=httpx.Response(401, request=request),
                        )

                    async def close(self) -> None:
                        return None

                class _HealthyTransport(McpTransport):
                    def __init__(self, *, on_message, on_diagnostic) -> None:  # type: ignore[no-untyped-def]
                        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)

                    async def start(self) -> None:
                        return None

                    async def send(self, message: dict[str, Any]) -> None:
                        if message.get("method") == "initialize" and "id" in message:
                            await self.emit_message(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "result": {
                                        "protocolVersion": "2025-11-25",
                                        "serverInfo": {"name": "Notion", "version": "1.0.0"},
                                        "capabilities": {},
                                    },
                                }
                            )

                    async def close(self) -> None:
                        return None

                async def _fake_build_transport(  # type: ignore[no-untyped-def]
                    *,
                    context,
                    server,
                    tool_executor,
                    on_message,
                    on_diagnostic,
                    runtime_transport=None,
                    url_override="",
                ):
                    del context, server, tool_executor
                    attempts.append((runtime_transport, url_override))
                    if runtime_transport == "sse":
                        return _HealthyTransport(
                            on_message=on_message,
                            on_diagnostic=on_diagnostic,
                        )
                    return _FailingTransport(
                        on_message=on_message,
                        on_diagnostic=on_diagnostic,
                    )

                manager._build_transport = _fake_build_transport  # type: ignore[method-assign]
                manager._refresh_tools = _noop_refresh  # type: ignore[method-assign]
                manager._refresh_resources = _noop_refresh  # type: ignore[method-assign]

                payload = await manager.prepare_agent(
                    session=session,
                    agent=agent,
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    tool_executor=object(),
                )

                self.assertTrue(payload["entries"][0]["connected"])
                self.assertEqual(payload["entries"][0]["transport"], "sse")
                self.assertEqual(
                    attempts,
                    [
                        ("streamable_http", ""),
                        ("sse", "https://mcp.notion.com/sse"),
                    ],
                )
                self.assertIn(
                    "mcp_transport_fallback_succeeded",
                    [event for event, _payload in diagnostics],
                )

        asyncio.run(run())

    def test_list_resources_paginates_and_filters_by_server(self) -> None:
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
                    enabled_server_ids=["filesystem", "docs"],
                    servers={
                        "filesystem": _AgentServerContext(
                            server=McpServerConfig(id="filesystem"),
                            runtime_state=McpServerRuntimeState(
                                server_id="filesystem",
                                title="filesystem",
                                transport="stdio",
                                enabled=True,
                            ),
                            resources=[
                                McpResourceDescriptor(
                                    server_id="filesystem",
                                    server_title="filesystem",
                                    uri="file:///a.txt",
                                ),
                                McpResourceDescriptor(
                                    server_id="filesystem",
                                    server_title="filesystem",
                                    uri="file:///b.txt",
                                ),
                            ],
                        ),
                        "docs": _AgentServerContext(
                            server=McpServerConfig(id="docs"),
                            runtime_state=McpServerRuntimeState(
                                server_id="docs",
                                title="docs",
                                transport="streamable_http",
                                enabled=True,
                            ),
                            resources=[
                                McpResourceDescriptor(
                                    server_id="docs",
                                    server_title="docs",
                                    uri="file:///doc.txt",
                                )
                            ],
                        ),
                    },
                )
                manager._agent_contexts[("session-1", "agent-1")] = context  # type: ignore[attr-defined]

                filtered = await manager.list_resources(
                    session_id="session-1",
                    agent_id="agent-1",
                    server_id="filesystem",
                    cursor=1,
                    limit=1,
                )
                unfiltered = await manager.list_resources(
                    session_id="session-1",
                    agent_id="agent-1",
                    server_id=None,
                    cursor=0,
                    limit=2,
                )

                self.assertEqual(filtered["mcp_resources_count"], 1)
                self.assertEqual(filtered["mcp_resources"][0]["uri"], "file:///b.txt")
                self.assertFalse(filtered["has_more"])
                self.assertEqual(unfiltered["mcp_resources_count"], 2)
                self.assertTrue(unfiltered["has_more"])
                self.assertEqual(unfiltered["next_cursor"], "2")

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
                self.assertEqual(healthy_payload["entries"][0]["tool_names"], ["search"])
                self.assertEqual(healthy_payload["entries"][0]["resource_uris"], ["file:///demo.txt"])
                self.assertEqual(healthy_payload["entries"][0]["tool_items"][0]["tool_name"], "search")
                self.assertEqual(
                    healthy_payload["entries"][0]["resource_items"][0]["uri"], "file:///demo.txt"
                )

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
                self.assertEqual(degraded_payload["entries"][0]["tool_names"], [])
                self.assertEqual(degraded_payload["entries"][0]["resource_uris"], [])
                self.assertEqual(degraded_payload["entries"][0]["tool_items"], [])
                self.assertEqual(degraded_payload["entries"][0]["resource_items"], [])
                self.assertEqual(
                    degraded_payload["warnings"][0]["message"],
                    "filesystem server unavailable",
                )

        asyncio.run(run())

    def test_read_resource_and_dynamic_tool_preserve_sanitized_payloads_and_log_diagnostics(
        self,
    ) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
                manager, diagnostics = self._manager_with_diagnostics(project_dir)
                context = _AgentMcpContext(
                    session_id="session-1",
                    agent_id="agent-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["filesystem"],
                )
                descriptor = McpToolDescriptor(
                    server_id="filesystem",
                    server_title="Filesystem",
                    tool_name="search",
                    synthetic_name=synthetic_tool_name("filesystem", "search"),
                )
                server_context = _AgentServerContext(
                    server=McpServerConfig(id="filesystem", transport="streamable_http", url="http://127.0.0.1:8787/mcp"),
                    runtime_state=McpServerRuntimeState(
                        server_id="filesystem",
                        title="Filesystem",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                    ),
                    tool_descriptors=[descriptor],
                    tool_by_synthetic_name={descriptor.synthetic_name: descriptor},
                    resources=[
                        McpResourceDescriptor(
                            server_id="filesystem",
                            server_title="Filesystem",
                            uri="file:///demo.txt",
                        )
                    ],
                )
                server_context.session = object()  # type: ignore[assignment]
                context.servers["filesystem"] = server_context
                manager._agent_contexts[("session-1", "agent-1")] = context  # type: ignore[attr-defined]

                async def _fake_request_with_reconnect(*, context, server_context, method, params):  # type: ignore[no-untyped-def]
                    del context, server_context
                    if method == "resources/read":
                        self.assertEqual(params, {"uri": "file:///demo.txt"})
                        return {
                            "contents": [
                                {"text": "x" * (MCP_MAX_INLINE_TEXT_CHARS + 1)},
                                {"blob": "a" * (MCP_MAX_INLINE_BINARY_BYTES + 1)},
                            ]
                        }
                    if method == "tools/call":
                        self.assertEqual(
                            params,
                            {"name": "search", "arguments": {"query": "demo"}},
                        )
                        return {
                            "content": [{"text": "ok"}],
                            "structuredContent": {
                                "summary": "x" * (MCP_MAX_INLINE_TEXT_CHARS + 1),
                            },
                        }
                    raise AssertionError(method)

                manager._request_with_reconnect = _fake_request_with_reconnect  # type: ignore[method-assign]

                resource_payload = await manager.read_resource(
                    session_id="session-1",
                    agent_id="agent-1",
                    uri="file:///demo.txt",
                    server_id=None,
                )
                tool_payload = await manager.call_dynamic_tool(
                    session_id="session-1",
                    agent_id="agent-1",
                    synthetic_name=descriptor.synthetic_name,
                    arguments={"query": "demo"},
                )

                self.assertTrue(resource_payload["contents"][0]["truncated"])
                self.assertTrue(resource_payload["contents"][1]["blob_omitted"])
                self.assertEqual(tool_payload["content"][0]["text"], "ok")
                self.assertEqual(
                    tool_payload["structured_content"]["summary"]["truncated"],
                    True,
                )
                self.assertEqual(
                    [event for event, _payload in diagnostics[-2:]],
                    ["mcp_resource_read", "mcp_tool_called"],
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

    def test_close_agent_and_close_session_close_connected_server_sessions(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
                manager = self._manager(project_dir)
                closed: list[str] = []

                class _Session:
                    def __init__(self, label: str) -> None:
                        self.label = label

                    async def close(self) -> None:
                        closed.append(self.label)

                for agent_id in ("agent-1", "agent-2"):
                    context = _AgentMcpContext(
                        session_id="session-1",
                        agent_id=agent_id,
                        workspace_path=project_dir,
                        workspace_is_remote=False,
                        enabled_server_ids=["filesystem"],
                        servers={
                            "filesystem": _AgentServerContext(
                                server=McpServerConfig(id="filesystem"),
                                runtime_state=McpServerRuntimeState(
                                    server_id="filesystem",
                                    title="filesystem",
                                    transport="stdio",
                                    enabled=True,
                                    connected=True,
                                ),
                                session=_Session(agent_id),  # type: ignore[arg-type]
                            )
                        },
                    )
                    manager._agent_contexts[("session-1", agent_id)] = context  # type: ignore[attr-defined]

                await manager.close_agent(session_id="session-1", agent_id="agent-1")
                await manager.close_session("session-1")

                self.assertEqual(closed, ["agent-1", "agent-2"])
                self.assertEqual(manager._agent_contexts, {})  # type: ignore[attr-defined]

        asyncio.run(run())

    def test_close_agent_defers_shared_session_shutdown_until_last_agent(self) -> None:
        async def run() -> None:
            with TemporaryDirectory() as temp_dir:
                project_dir = Path(temp_dir)
                (project_dir / "opencompany.toml").write_text("", encoding="utf-8")
                manager = self._manager(project_dir)
                closed: list[str] = []

                class _Session:
                    async def close(self) -> None:
                        closed.append("closed")

                shared_servers = {
                    "filesystem": _AgentServerContext(
                        server=McpServerConfig(id="filesystem"),
                        runtime_state=McpServerRuntimeState(
                            server_id="filesystem",
                            title="filesystem",
                            transport="stdio",
                            enabled=True,
                            connected=True,
                        ),
                        session=_Session(),  # type: ignore[arg-type]
                    )
                }
                manager._session_contexts["session-1"] = _SessionMcpContext(  # type: ignore[attr-defined]
                    session_id="session-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["filesystem"],
                    servers=shared_servers,
                )
                manager._agent_contexts[("session-1", "agent-1")] = _AgentMcpContext(  # type: ignore[attr-defined]
                    session_id="session-1",
                    agent_id="agent-1",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["filesystem"],
                    servers=shared_servers,
                )
                manager._agent_contexts[("session-1", "agent-2")] = _AgentMcpContext(  # type: ignore[attr-defined]
                    session_id="session-1",
                    agent_id="agent-2",
                    workspace_path=project_dir,
                    workspace_is_remote=False,
                    enabled_server_ids=["filesystem"],
                    servers=shared_servers,
                )

                await manager.close_agent(session_id="session-1", agent_id="agent-1")
                self.assertEqual(closed, [])
                await manager.close_agent(session_id="session-1", agent_id="agent-2")
                self.assertEqual(closed, ["closed"])

        asyncio.run(run())

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

    def test_request_with_reconnect_retries_after_session_not_found(self) -> None:
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
                server_context = _AgentServerContext(
                    server=McpServerConfig(id="filesystem", transport="streamable_http", url="http://127.0.0.1:8787/mcp"),
                    runtime_state=McpServerRuntimeState(
                        server_id="filesystem",
                        title="filesystem",
                        transport="streamable_http",
                        enabled=True,
                        connected=True,
                    ),
                )

                class _MissingSession:
                    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
                        del method, params
                        raise McpError("MCP HTTP session was not found (404).")

                    async def close(self) -> None:
                        return None

                class _HealthySession:
                    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
                        return {"method": method, "params": params or {}}

                    async def close(self) -> None:
                        return None

                server_context.session = _MissingSession()
                context.servers["filesystem"] = server_context

                async def _fake_reconnect_server_context(*, context, server_context):  # type: ignore[no-untyped-def]
                    del context
                    server_context.session = _HealthySession()
                    server_context.runtime_state.connected = True
                    server_context.runtime_state.warning = ""

                manager._reconnect_server_context = _fake_reconnect_server_context  # type: ignore[method-assign]

                result = await manager._request_with_reconnect(  # type: ignore[attr-defined]
                    context=context,
                    server_context=server_context,
                    method="tools/list",
                    params={"cursor": "1"},
                )

                self.assertEqual(
                    result,
                    {"method": "tools/list", "params": {"cursor": "1"}},
                )
                self.assertTrue(server_context.runtime_state.connected)
                self.assertEqual(server_context.runtime_state.warning, "")

        asyncio.run(run())
