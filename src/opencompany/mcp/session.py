from __future__ import annotations

import abc
import asyncio
import contextlib
import json
from http import HTTPStatus
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from opencompany.config import MCP_PROTOCOL_VERSIONS
from opencompany.mcp.oauth import McpOAuthError, McpOAuthRequiredError, McpOAuthTokenProvider
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


@dataclass(slots=True)
class _NamedSseEvent:
    event: str
    data: str


class _NamedSseParser:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[_NamedSseEvent]:
        self._buffer += chunk
        events: list[_NamedSseEvent] = []
        while "\n\n" in self._buffer:
            raw_event, self._buffer = self._buffer.split("\n\n", 1)
            event_name = "message"
            lines: list[str] = []
            for line in raw_event.splitlines():
                if line.startswith("event:"):
                    normalized = line[6:].strip()
                    if normalized:
                        event_name = normalized
                    continue
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
            if lines:
                events.append(_NamedSseEvent(event=event_name, data="\n".join(lines)))
        return events


def normalize_streamable_http_url(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return normalized
    original_query = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_query = [
        (key, value)
        for key, value in original_query
        if str(key).strip().lower() != "login"
    ]
    if len(filtered_query) == len(original_query):
        return normalized
    query = urlencode(filtered_query, doseq=True) if filtered_query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def derive_legacy_sse_url(url: str) -> str:
    parsed = urlsplit(normalize_streamable_http_url(url))
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path or ""
    if path.endswith("/mcp"):
        path = f"{path[:-4]}/sse"
    elif not path or path == "/":
        path = "/sse"
    else:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _truncate_http_error_text(value: str, *, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 3]}..."


def _http_status_label(status_code: int, reason_phrase: str) -> str:
    reason = str(reason_phrase or "").strip()
    if not reason:
        with contextlib.suppress(Exception):
            reason = HTTPStatus(int(status_code)).phrase
    return f"{status_code} {reason}".strip() if reason else str(status_code)


def _http_error_message(*, response: Any, detail: str = "") -> str:
    status_code = int(getattr(response, "status_code", 0) or 0)
    reason_phrase = str(getattr(response, "reason_phrase", "") or "").strip()
    status_label = _http_status_label(status_code, reason_phrase)
    request = getattr(response, "request", None)
    url = ""
    if request is not None:
        with contextlib.suppress(Exception):
            url = str(request.url or "").strip()
    if 400 <= status_code < 500:
        prefix = "Client error"
    elif 500 <= status_code < 600:
        prefix = "Server error"
    else:
        prefix = "HTTP error"
    message = f"{prefix} '{status_label}'"
    if url:
        message = f"{message} for url '{url}'"
    if detail:
        message = f"{message}. {detail}"
    return message


def _http_error_detail(*, response: Any, raw_content: bytes) -> str:
    headers = getattr(response, "headers", {}) or {}
    www_authenticate = str(headers.get("WWW-Authenticate", "") or "").strip()
    content_type = str(headers.get("Content-Type", "") or "").lower()
    raw_text = raw_content.decode("utf-8", errors="replace").strip() if raw_content else ""
    body_detail = ""
    if raw_text:
        if "application/json" in content_type:
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(raw_text)
                if isinstance(payload, dict):
                    parts: list[str] = []
                    for key in ("error", "error_description", "message", "detail", "code"):
                        value = str(payload.get(key, "") or "").strip()
                        if value:
                            parts.append(f"{key}={value}")
                    if parts:
                        body_detail = ", ".join(parts)
        if not body_detail:
            body_detail = raw_text
    fragments: list[str] = []
    if www_authenticate:
        fragments.append(
            f"www-authenticate={_truncate_http_error_text(www_authenticate, max_chars=240)}"
        )
    if body_detail:
        fragments.append(f"response={_truncate_http_error_text(body_detail, max_chars=360)}")
    return "; ".join(fragments)


async def _raise_for_status_with_context(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 400:
        return
    raw_content = b""
    with contextlib.suppress(Exception):
        raw_content = await response.aread()
    detail = _http_error_detail(response=response, raw_content=raw_content)
    raise McpError(_http_error_message(response=response, detail=detail))


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
        oauth_provider: McpOAuthTokenProvider | None = None,
        user_agent: str = "OpenCompany/0.1.0",
    ) -> None:
        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)
        self.url = str(url)
        self.headers = dict(headers)
        self.protocol_version = str(protocol_version)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._oauth_provider = oauth_provider
        self._user_agent = str(user_agent or "OpenCompany/0.1.0").strip() or "OpenCompany/0.1.0"
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
        for attempt in range(2):
            headers = await self._request_headers()
            async with self._client.stream(
                "POST",
                self.url,
                headers=headers,
                json=message,
            ) as response:
                self._capture_session_headers(response)
                if response.status_code == 404 and self._session_id:
                    raise McpError("MCP HTTP session was not found (404).")
                if response.status_code == 401 and await self._handle_unauthorized(
                    response=response,
                    attempt=attempt,
                    failed_authorization=headers.get("Authorization", ""),
                ):
                    continue
                await _raise_for_status_with_context(response)
                await self._consume_http_response(response)
                return

    async def maybe_start_reader(self) -> None:
        if self._reader_task is not None or not self._session_id:
            return
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        assert self._client is not None
        try:
            for attempt in range(2):
                headers = await self._request_headers(accept="text/event-stream")
                async with self._client.stream("GET", self.url, headers=headers) as response:
                    if response.status_code in {404, 405}:
                        await self.emit_diagnostic(
                            "mcp_http_reader_unavailable",
                            {"status_code": response.status_code},
                        )
                        return
                    if response.status_code == 401 and await self._handle_unauthorized(
                        response=response,
                        attempt=attempt,
                        failed_authorization=headers.get("Authorization", ""),
                    ):
                        continue
                    await _raise_for_status_with_context(response)
                    self._capture_session_headers(response)
                    parser = _SseParser()
                    async for chunk in response.aiter_text():
                        for event_data in parser.feed(chunk):
                            if event_data == "[DONE]":
                                continue
                            await self._handle_event_data(event_data)
                    return
        except Exception as exc:
            await self.emit_diagnostic(
                "mcp_http_reader_failed",
                {"error": str(exc)},
            )

    async def _request_headers(self, *, accept: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept or "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            "User-Agent": self._user_agent,
            **self.headers,
        }
        if self._oauth_provider is not None:
            try:
                headers["Authorization"] = await self._oauth_provider.authorization_header()
            except McpOAuthRequiredError as exc:
                raise McpError(str(exc)) from exc
            except McpOAuthError as exc:
                raise McpError(str(exc)) from exc
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _handle_unauthorized(
        self,
        *,
        response: httpx.Response,
        attempt: int,
        failed_authorization: str = "",
    ) -> bool:
        if self._oauth_provider is None or attempt > 0:
            return False
        try:
            refreshed = await self._oauth_provider.refresh_on_unauthorized(
                failed_authorization=failed_authorization,
            )
        except McpOAuthRequiredError as exc:
            raise McpError(str(exc)) from exc
        except McpOAuthError as exc:
            raise McpError(str(exc)) from exc
        if not refreshed:
            resource_metadata = str(
                response.headers.get("WWW-Authenticate", "") or ""
            ).strip()
            if resource_metadata:
                await self.emit_diagnostic(
                    "mcp_http_unauthorized",
                    {"www_authenticate": resource_metadata},
                )
            return False
        await self.emit_diagnostic(
            "mcp_http_unauthorized",
            {"refreshed": True},
        )
        return True

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
                    await self._client.delete(
                        self.url,
                        headers=await self._request_headers(),
                    )
            await self._client.aclose()
            self._client = None


class LegacySseMcpTransport(McpTransport):
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
        on_message: MessageCallback,
        on_diagnostic: DiagnosticCallback,
        oauth_provider: McpOAuthTokenProvider | None = None,
        user_agent: str = "OpenCompany/0.1.0",
    ) -> None:
        super().__init__(on_message=on_message, on_diagnostic=on_diagnostic)
        self.url = str(url)
        self.headers = dict(headers)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._oauth_provider = oauth_provider
        self._user_agent = str(user_agent or "OpenCompany/0.1.0").strip() or "OpenCompany/0.1.0"
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_ready = asyncio.Event()
        self._reader_error: Exception | None = None
        self._post_url = ""

    @property
    def post_url(self) -> str:
        return self._post_url

    async def start(self) -> None:
        if self._closed:
            raise McpError("MCP legacy SSE transport is closed.")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        if self._reader_task is not None:
            return
        self._reader_ready.clear()
        self._reader_error = None
        self._reader_task = asyncio.create_task(self._reader_loop())
        try:
            await asyncio.wait_for(self._reader_ready.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise McpError(
                f"MCP legacy SSE transport did not advertise a message endpoint within {self.timeout_seconds:.2f}s."
            ) from exc
        if self._reader_error is not None:
            raise self._reader_error
        if not self._post_url:
            raise McpError("MCP legacy SSE transport did not advertise a message endpoint.")

    async def send(self, message: JsonRpcMessage) -> None:
        if self._closed:
            raise McpError("MCP legacy SSE transport is closed.")
        await self.start()
        if not self._post_url:
            raise McpError("MCP legacy SSE transport is missing a message endpoint.")
        assert self._client is not None
        for attempt in range(2):
            headers = await self._request_headers()
            async with self._client.stream(
                "POST",
                self._post_url,
                headers=headers,
                json=message,
            ) as response:
                if response.status_code == 401 and await self._handle_unauthorized(
                    response=response,
                    attempt=attempt,
                    failed_authorization=headers.get("Authorization", ""),
                ):
                    continue
                await _raise_for_status_with_context(response)
                await self._consume_http_response(response)
                return

    async def _reader_loop(self) -> None:
        assert self._client is not None
        try:
            for attempt in range(2):
                headers = await self._request_headers(accept="text/event-stream")
                async with self._client.stream("GET", self.url, headers=headers) as response:
                    if response.status_code == 401 and await self._handle_unauthorized(
                        response=response,
                        attempt=attempt,
                        failed_authorization=headers.get("Authorization", ""),
                    ):
                        continue
                    await _raise_for_status_with_context(response)
                    parser = _NamedSseParser()
                    async for chunk in response.aiter_text():
                        for event in parser.feed(chunk):
                            await self._handle_named_event(event)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._reader_ready.is_set():
                self._reader_error = exc
                self._reader_ready.set()
            else:
                await self.emit_diagnostic(
                    "mcp_sse_reader_failed",
                    {"error": str(exc)},
                )
            return
        if not self._reader_ready.is_set():
            self._reader_error = McpError(
                "MCP legacy SSE transport closed before advertising a message endpoint."
            )
            self._reader_ready.set()

    async def _handle_named_event(self, event: _NamedSseEvent) -> None:
        if event.event == "endpoint":
            self._post_url = urljoin(self.url, event.data.strip())
            if not self._reader_ready.is_set():
                self._reader_ready.set()
            return
        if not self._reader_ready.is_set() and self._post_url:
            self._reader_ready.set()
        if event.data == "[DONE]":
            return
        await self._handle_event_data(event.data)

    async def _request_headers(self, *, accept: str | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept or "application/json, text/event-stream",
            "User-Agent": self._user_agent,
            **self.headers,
        }
        if self._oauth_provider is not None:
            try:
                headers["Authorization"] = await self._oauth_provider.authorization_header()
            except McpOAuthRequiredError as exc:
                raise McpError(str(exc)) from exc
            except McpOAuthError as exc:
                raise McpError(str(exc)) from exc
        return headers

    async def _handle_unauthorized(
        self,
        *,
        response: httpx.Response,
        attempt: int,
        failed_authorization: str = "",
    ) -> bool:
        if self._oauth_provider is None or attempt > 0:
            return False
        try:
            refreshed = await self._oauth_provider.refresh_on_unauthorized(
                failed_authorization=failed_authorization,
            )
        except McpOAuthRequiredError as exc:
            raise McpError(str(exc)) from exc
        except McpOAuthError as exc:
            raise McpError(str(exc)) from exc
        if not refreshed:
            return False
        await self.emit_diagnostic(
            "mcp_http_unauthorized",
            {"refreshed": True},
        )
        return True

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
