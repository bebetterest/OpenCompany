from __future__ import annotations

import abc
import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from opencompany.config import MCP_PROTOCOL_VERSIONS
from opencompany.sandbox.base import InteractiveSandboxProcess

JsonRpcMessage = dict[str, Any]
MessageCallback = Callable[[JsonRpcMessage], Awaitable[None] | None]
DiagnosticCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class McpError(RuntimeError):
    pass


class McpProtocolError(McpError):
    pass


class McpRequestError(McpError):
    def __init__(self, *, code: int, message: str, data: Any = None) -> None:
        self.code = int(code)
        self.data = data
        super().__init__(message)


class _SseParser:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self._buffer += chunk
        events: list[str] = []
        while "\n\n" in self._buffer:
            raw_event, self._buffer = self._buffer.split("\n\n", 1)
            lines = []
            for line in raw_event.splitlines():
                if line.startswith("data:"):
                    lines.append(line[5:].strip())
            if lines:
                events.append("\n".join(lines))
        return events


class McpTransport(abc.ABC):
    def __init__(
        self,
        *,
        on_message: MessageCallback,
        on_diagnostic: DiagnosticCallback,
    ) -> None:
        self._on_message = on_message
        self._on_diagnostic = on_diagnostic

    async def emit_message(self, message: JsonRpcMessage) -> None:
        maybe = self._on_message(message)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def emit_diagnostic(self, event_type: str, payload: dict[str, Any]) -> None:
        maybe = self._on_diagnostic(event_type, payload)
        if asyncio.iscoroutine(maybe):
            await maybe

    @abc.abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def send(self, message: JsonRpcMessage) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class StdioMcpTransport(McpTransport):
    def __init__(
        self,
        *,
        process: InteractiveSandboxProcess,
        on_message: MessageCallback,
        on_diagnostic: DiagnosticCallback,
    ) -> None:
        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)
        self._process = process
        self._closed = False

    async def start(self) -> None:
        return None

    async def send(self, message: JsonRpcMessage) -> None:
        if self._closed:
            raise McpError("MCP stdio transport is closed.")
        await self._process.write_line(
            json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        )

    async def handle_stdout(self, text: str) -> None:
        normalized = str(text or "").strip()
        if not normalized:
            return
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(f"Invalid MCP JSON line: {exc}") from exc
        if not isinstance(payload, dict):
            raise McpProtocolError("MCP stdio payload must be a JSON object.")
        await self.emit_message(payload)

    async def handle_stderr(self, text: str) -> None:
        await self.emit_diagnostic(
            "mcp_stdio_stderr",
            {"text": str(text or "")},
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._process.close()


class StreamableHttpMcpTransport(McpTransport):
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        protocol_version: str,
        timeout_seconds: float,
        on_message: MessageCallback,
        on_diagnostic: DiagnosticCallback,
    ) -> None:
        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)
        self.url = str(url)
        self.headers = dict(headers)
        self.protocol_version = str(protocol_version)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._session_id: str = ""
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)

    async def send(self, message: JsonRpcMessage) -> None:
        if self._closed:
            raise McpError("MCP HTTP transport is closed.")
        await self.start()
        assert self._client is not None
        headers = self._request_headers()
        async with self._client.stream(
            "POST",
            self.url,
            headers=headers,
            json=message,
        ) as response:
            self._capture_session_headers(response)
            if response.status_code == 404 and self._session_id:
                raise McpError("MCP HTTP session was not found (404).")
            response.raise_for_status()
            await self._consume_http_response(response)

    async def maybe_start_reader(self) -> None:
        if self._reader_task is not None or not self._session_id:
            return
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        assert self._client is not None
        headers = self._request_headers(accept="text/event-stream")
        try:
            async with self._client.stream("GET", self.url, headers=headers) as response:
                if response.status_code in {404, 405}:
                    await self.emit_diagnostic(
                        "mcp_http_reader_unavailable",
                        {"status_code": response.status_code},
                    )
                    return
                response.raise_for_status()
                self._capture_session_headers(response)
                parser = _SseParser()
                async for chunk in response.aiter_text():
                    for event_data in parser.feed(chunk):
                        if event_data == "[DONE]":
                            continue
                        await self._handle_event_data(event_data)
        except Exception as exc:
            await self.emit_diagnostic(
                "mcp_http_reader_failed",
                {"error": str(exc)},
            )

    def _request_headers(self, *, accept: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept or "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _capture_session_headers(self, response: httpx.Response) -> None:
        session_id = str(response.headers.get("Mcp-Session-Id", "")).strip()
        if session_id:
            self._session_id = session_id

    async def _consume_http_response(self, response: httpx.Response) -> None:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if "text/event-stream" in content_type:
            parser = _SseParser()
            async for chunk in response.aiter_text():
                for event_data in parser.feed(chunk):
                    if event_data == "[DONE]":
                        continue
                    await self._handle_event_data(event_data)
            return
        raw_content = await response.aread()
        if not raw_content:
            return
        try:
            payload = json.loads(raw_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpProtocolError(f"Invalid MCP HTTP response payload: {exc}") from exc
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    await self.emit_message(item)
            return
        if isinstance(payload, dict):
            await self.emit_message(payload)

    async def _handle_event_data(self, event_data: str) -> None:
        try:
            payload = json.loads(event_data)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(f"Invalid MCP HTTP event payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise McpProtocolError("MCP HTTP event payload must be a JSON object.")
        await self.emit_message(payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._client is not None:
            if self._session_id:
                with contextlib.suppress(Exception):
                    await self._client.delete(self.url, headers=self._request_headers())
            await self._client.aclose()
            self._client = None


@dataclass(slots=True)
class McpInitialization:
    server_name: str
    server_version: str
    protocol_version: str
    capabilities: dict[str, Any]


class McpClientSession:
    def __init__(
        self,
        *,
        transport: McpTransport,
        protocol_version: str,
        request_timeout_seconds: float,
        client_name: str,
        client_version: str,
        roots_provider: Callable[[], list[dict[str, Any]]],
        on_notification: Callable[[str, dict[str, Any] | None], Awaitable[None] | None],
        on_diagnostic: DiagnosticCallback,
    ) -> None:
        self.transport = transport
        self.protocol_version = str(protocol_version)
        self.request_timeout_seconds = max(0.01, float(request_timeout_seconds))
        self.client_name = client_name
        self.client_version = client_version
        self.roots_provider = roots_provider
        self.on_notification = on_notification
        self.on_diagnostic = on_diagnostic
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._next_id = 0
        self._closed = False
        self.initialization: McpInitialization | None = None

    async def initialize(self, *, roots_enabled: bool) -> McpInitialization:
        await self.transport.start()
        result = await self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    **({"roots": {"listChanged": False}} if roots_enabled else {}),
                },
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        protocol_version = str(result.get("protocolVersion", self.protocol_version)).strip() or self.protocol_version
        if protocol_version not in MCP_PROTOCOL_VERSIONS:
            raise McpProtocolError(
                f"Unsupported MCP protocol version returned by server: {protocol_version}."
            )
        server_info = result.get("serverInfo")
        server_name = ""
        server_version = ""
        if isinstance(server_info, dict):
            server_name = str(server_info.get("name", "")).strip()
            server_version = str(server_info.get("version", "")).strip()
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        self.initialization = McpInitialization(
            server_name=server_name,
            server_version=server_version,
            protocol_version=protocol_version,
            capabilities=capabilities,
        )
        if isinstance(self.transport, StreamableHttpMcpTransport):
            self.transport.protocol_version = protocol_version
        await self.notify("notifications/initialized", {})
        if isinstance(self.transport, StreamableHttpMcpTransport):
            await self.transport.maybe_start_reader()
        return self.initialization

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_request_id()
        pending_key = self._pending_key(request_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[pending_key] = future
        async with self._send_lock:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": str(method),
                    **({"params": params} if params is not None else {}),
                }
            )
        try:
            return await asyncio.wait_for(
                future,
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise McpError(
                f"MCP request '{method}' timed out after {self.request_timeout_seconds:.2f}s."
            ) from exc
        finally:
            self._pending.pop(pending_key, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        async with self._send_lock:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "method": str(method),
                    **({"params": params} if params is not None else {}),
                }
            )

    async def handle_message(self, message: JsonRpcMessage) -> None:
        if "id" in message and ("result" in message or "error" in message):
            pending_key = self._pending_key(message.get("id"))
            if pending_key is None:
                return
            future = self._pending.get(pending_key)
            if future is None or future.done():
                return
            if isinstance(message.get("error"), dict):
                error = message["error"]
                future.set_exception(
                    McpRequestError(
                        code=int(error.get("code", -32000)),
                        message=str(error.get("message", "MCP request failed.")),
                        data=error.get("data"),
                    )
                )
                return
            result = message.get("result")
            if not isinstance(result, dict):
                result = {}
            future.set_result(result)
            return
        method = str(message.get("method", "")).strip()
        if not method:
            return
        params = message.get("params")
        normalized_params = params if isinstance(params, dict) else {}
        if "id" in message:
            await self._handle_request(
                request_id=message["id"],
                method=method,
                params=normalized_params,
            )
            return
        maybe = self.on_notification(method, normalized_params)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def _handle_request(
        self,
        *,
        request_id: Any,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            if method == "roots/list":
                result = {"roots": list(self.roots_provider())}
            elif method == "ping":
                result = {}
            else:
                raise McpRequestError(code=-32601, message=f"Unsupported MCP method '{method}'.")
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            )
        except McpRequestError as exc:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        **({"data": exc.data} if exc.data is not None else {}),
                    },
                }
            )
        except Exception as exc:
            await self.transport.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": str(exc) or "Internal MCP error.",
                    },
                }
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.transport.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError("MCP session closed."))
        self._pending.clear()

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _pending_key(value: Any) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return str(value)
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return None
